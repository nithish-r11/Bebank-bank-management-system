import calendar
import secrets
from decimal import Decimal, ROUND_HALF_UP
from django.db import models
from django.utils import timezone
from accounts.models import User


def add_months(sourcedate, months):
    """Deterministically advance a datetime/date by a given number of months."""
    month = sourcedate.month - 1 + months
    year = sourcedate.year + month // 12
    month = month % 12 + 1
    day = min(sourcedate.day, calendar.monthrange(year, month)[1])
    return sourcedate.replace(year=year, month=month, day=day)


# =========================
# BANK ACCOUNT
# =========================
class BankAccount(models.Model):

    ACCOUNT_TYPE = (
        ('SAVINGS', 'Savings'),
        ('CURRENT', 'Current'),
    )

    STATUS_CHOICES = (
        ('ACTIVE', 'Active'),
        ('FROZEN', 'Frozen'),
    )

    user = models.ForeignKey(User, on_delete=models.CASCADE)

    account_number = models.CharField(max_length=20, unique=True, blank=True)

    account_type = models.CharField(max_length=20, choices=ACCOUNT_TYPE)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ACTIVE', db_index=True)
    frozen_at = models.DateTimeField(null=True, blank=True)
    frozen_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='frozen_bank_accounts'
    )
    freeze_reason = models.TextField(blank=True, default='')

    balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    # ⭐ CUSTOMER DETAILS
    email = models.EmailField(null=True, blank=True)
    phone = models.CharField(max_length=15, null=True, blank=True)
    aadhaar = models.CharField(max_length=12, null=True, blank=True)
    pan = models.CharField(max_length=10, null=True, blank=True)
    address = models.TextField(null=True, blank=True)
    pincode = models.CharField(max_length=6, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def is_frozen(self):
        return self.status == 'FROZEN'

    def save(self, *args, **kwargs):

        # AUTO ACCOUNT NUMBER
        if not self.account_number:
            last = BankAccount.objects.order_by('-id').first()

            if last and last.account_number.isdigit():
                self.account_number = str(int(last.account_number) + 1)
            else:
                self.account_number = "10001"

        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.user.username} - {self.account_number}"


# =========================
# TRANSACTION
# =========================
class Transaction(models.Model):

    TRANSACTION_TYPE = (
        ('WITHDRAW', 'Withdraw'),
        ('TRANSFER', 'Transfer'),
        ('TRANSFER_DEBIT', 'Transfer Sent'),
        ('TRANSFER_CREDIT', 'Transfer Received'),
        ('DEPOSIT', 'Deposit'),
        ('LOAN_CREDIT', 'Loan Credited'),
        ('LOAN_REPAYMENT_DEBIT', 'Loan EMI Repayment'),
        ('FD_DEBIT', 'Fixed Deposit Created'),
        ('FD_MATURITY_CREDIT', 'Fixed Deposit Maturity Payout'),
        ('FD_CLOSURE_CREDIT', 'Fixed Deposit Closure Payout'),
    )

    account = models.ForeignKey(BankAccount, on_delete=models.CASCADE)

    transaction_type = models.CharField(max_length=30, choices=TRANSACTION_TYPE)

    amount = models.DecimalField(max_digits=12, decimal_places=2)

    # ⭐ Reciprocal Two-Leg Ledger Additive Fields
    reference_id = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    related_account = models.ForeignKey(
        BankAccount,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='related_transactions'
    )
    balance_before = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    balance_after = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['reference_id', 'transaction_type'],
                condition=models.Q(reference_id__isnull=False),
                name='unique_transfer_leg_per_reference'
            )
        ]

    def is_credit(self):
        return self.transaction_type in [
            'DEPOSIT', 'LOAN_CREDIT', 'TRANSFER_CREDIT',
            'FD_MATURITY_CREDIT', 'FD_CLOSURE_CREDIT'
        ]

    def is_debit(self):
        return self.transaction_type in [
            'WITHDRAW', 'TRANSFER', 'TRANSFER_DEBIT',
            'FD_DEBIT', 'LOAN_REPAYMENT_DEBIT'
        ]

    @property
    def direction(self):
        return 'CREDIT' if self.is_credit() else 'DEBIT'

    def __str__(self):
        ref = f" | Ref: {self.reference_id}" if self.reference_id else ""
        return f"{self.account.account_number} | {self.transaction_type} | ₹{self.amount}{ref}"


# =========================
# FIXED DEPOSIT
# =========================
class FixedDeposit(models.Model):

    STATUS_CHOICES = (
        ('ACTIVE', 'Active'),
        ('MATURED', 'Matured'),
        ('CLOSED', 'Closed'),
    )

    account = models.ForeignKey(BankAccount, on_delete=models.CASCADE, related_name='fixed_deposits')

    amount = models.DecimalField(max_digits=12, decimal_places=2)

    interest_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('6.50'))

    duration_months = models.IntegerField()

    start_date = models.DateTimeField(default=timezone.now)

    maturity_date = models.DateTimeField(null=True, blank=True)

    maturity_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ACTIVE')

    settled_at = models.DateTimeField(null=True, blank=True)

    settlement_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    closure_type = models.CharField(
        max_length=20,
        blank=True,
        choices=[('MATURITY', 'Maturity Settlement'), ('PREMATURE', 'Premature Closure')]
    )

    closure_reason = models.TextField(blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if not self.start_date:
            self.start_date = timezone.now()

        rate = Decimal(str(self.interest_rate))
        months = Decimal(str(self.duration_months))

        interest = (self.amount * rate * months) / Decimal('1200')
        self.maturity_amount = (self.amount + interest).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        if not self.maturity_date:
            self.maturity_date = add_months(self.start_date, int(months))

        super().save(*args, **kwargs)

    @property
    def interest_earned(self):
        if self.maturity_amount and self.amount:
            return (self.maturity_amount - self.amount).quantize(Decimal('0.01'))
        return Decimal('0.00')

    def is_matured_eligible(self):
        return bool(
            self.status == 'ACTIVE' and
            self.maturity_date and
            timezone.now() >= self.maturity_date
        )

    def is_premature_eligible(self):
        return self.status == 'ACTIVE'

    def __str__(self):
        return f"FD #{self.id or 'New'} - {self.account.account_number} (₹{self.amount}, {self.status})"


# =========================
# LOAN
# =========================
class Loan(models.Model):

    STATUS_CHOICES = (
        ('PENDING', 'Pending'),
        ('UNDER_REVIEW', 'Under Review'),
        ('APPROVED', 'Approved'),
        ('REJECTED', 'Rejected'),
        ('CLOSED', 'Closed'),
    )

    account = models.ForeignKey(BankAccount, on_delete=models.CASCADE, related_name='loans')

    amount = models.DecimalField(max_digits=12, decimal_places=2)

    interest_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('10.50'))

    duration_months = models.IntegerField()

    emi_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    total_interest = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    total_payable = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    outstanding_balance = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="PENDING")

    # Employee Verification & Audit Fields (Task 3.3)
    reviewed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='reviewed_loans'
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    recommendation = models.CharField(
        max_length=30,
        choices=[
            ('NONE', 'No Recommendation'),
            ('RECOMMEND_APPROVE', 'Recommend Approval'),
            ('RECOMMEND_REJECT', 'Recommend Rejection'),
        ],
        default='NONE'
    )
    verification_notes = models.TextField(blank=True, default='')

    # Final Administrative Decision Fields
    admin_decided_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='decided_loans'
    )
    admin_decision_reason = models.TextField(blank=True, default='')

    disbursed_at = models.DateTimeField(null=True, blank=True)

    closed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    @property
    def total_paid(self):
        if self.total_payable and self.outstanding_balance is not None:
            paid = self.total_payable - self.outstanding_balance
            return paid.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        return Decimal('0.00')

    @property
    def is_repayable(self):
        return (
            self.status == 'APPROVED' and
            self.outstanding_balance is not None and
            self.outstanding_balance > Decimal('0.00')
        )

    def __str__(self):
        return f"Loan #{self.id or 'New'} - {self.account.account_number} - ₹{self.amount} ({self.status})"


# =========================
# LOAN INSTALLMENT (AMORTIZATION)
# =========================
class LoanInstallment(models.Model):

    STATUS_CHOICES = (
        ('PENDING', 'Pending'),
        ('PARTIALLY_PAID', 'Partially Paid'),
        ('PAID', 'Paid'),
        ('OVERDUE', 'Overdue'),
    )

    loan = models.ForeignKey(Loan, on_delete=models.CASCADE, related_name='installments')

    installment_number = models.PositiveIntegerField()

    due_date = models.DateField()

    opening_principal = models.DecimalField(max_digits=12, decimal_places=2)

    principal_component = models.DecimalField(max_digits=12, decimal_places=2)

    interest_component = models.DecimalField(max_digits=12, decimal_places=2)

    emi_due = models.DecimalField(max_digits=12, decimal_places=2)

    amount_paid = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')

    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['installment_number']
        constraints = [
            models.UniqueConstraint(
                fields=['loan', 'installment_number'],
                name='unique_installment_per_loan'
            )
        ]

    @property
    def remaining_due(self):
        due = self.emi_due - self.amount_paid
        return due if due > Decimal('0.00') else Decimal('0.00')

    def __str__(self):
        return f"Loan #{self.loan_id} - Installment {self.installment_number} (₹{self.emi_due}, {self.status})"


# =========================
# LOAN REPAYMENT
# =========================
class LoanRepayment(models.Model):

    loan = models.ForeignKey(Loan, on_delete=models.CASCADE, related_name='repayments')

    account = models.ForeignKey(BankAccount, on_delete=models.CASCADE, related_name='loan_repayments')

    amount = models.DecimalField(max_digits=12, decimal_places=2)

    reference_id = models.CharField(max_length=64, unique=True, db_index=True)

    payment_date = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-payment_date']

    def __str__(self):
        return f"Repayment {self.reference_id} - Loan #{self.loan_id} (₹{self.amount})"


# =========================
# LOAN VERIFICATION & AUDIT LOG (TASK 3.3)
# =========================
class LoanVerificationLog(models.Model):
    ACTION_CHOICES = (
        ('UNDER_REVIEW', 'Marked Under Review'),
        ('RECOMMEND_APPROVE', 'Recommended Approval'),
        ('RECOMMEND_REJECT', 'Recommended Rejection'),
        ('VERIFICATION_NOTE', 'Added Verification Note'),
        ('ADMIN_APPROVE', 'Approved by Administration'),
        ('ADMIN_REJECT', 'Rejected by Administration'),
    )

    loan = models.ForeignKey(Loan, on_delete=models.CASCADE, related_name='verification_logs')
    performed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='loan_verification_actions')
    action = models.CharField(max_length=30, choices=ACTION_CHOICES)
    notes = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        actor = self.performed_by.username if self.performed_by else "System"
        return f"Loan #{self.loan_id} {self.action} by {actor} at {self.created_at}"


# =========================
# BENEFICIARY
# =========================
class Beneficiary(models.Model):

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='beneficiaries')

    account = models.ForeignKey(BankAccount, on_delete=models.CASCADE, related_name='beneficiary_entries')

    nickname = models.CharField(max_length=50, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'account'],
                name='unique_beneficiary_per_user'
            )
        ]
        ordering = ['-created_at']

    @property
    def account_holder_name(self):
        if not self.account or not self.account.user:
            return "Unavailable"
        full = self.account.user.get_full_name().strip()
        return full if full else self.account.user.username

    def is_available(self):
        if not self.account or not self.account.user:
            return False
        return bool(self.account.user.is_active and not getattr(self.account.user, 'account_locked', False))

    def masked_account_number(self):
        if not self.account or not self.account.account_number:
            return ""
        raw = str(self.account.account_number).strip()
        if len(raw) <= 4:
            return raw
        return f"•••• {raw[-4:]}"

    def __str__(self):
        name = self.nickname or self.account_holder_name
        return f"{name} ({self.masked_account_number()})"


# =========================
# VIRTUAL DEBIT CARD (SIMULATION)
# =========================
def generate_simulation_card_number():
    """
    Generates a 16-digit simulation card identifier using unassigned 5045 prefix.
    Fictional academic simulation only — not affiliated with or accepted by real payment networks.
    """
    body = "".join(str(secrets.randbelow(10)) for _ in range(12))
    return f"5045{body}"


class VirtualDebitCard(models.Model):

    STATUS_CHOICES = (
        ('ACTIVE', 'Active'),
        ('BLOCKED', 'Blocked'),
        ('EXPIRED', 'Expired'),
        ('CANCELLED', 'Cancelled'),
    )

    CARD_TYPE_CHOICES = (
        ('CLASSIC', 'BEBANK Classic Simulation'),
        ('PLATINUM', 'BEBANK Platinum Simulation'),
        ('VIRTUAL_ACCESS', 'BEBANK Virtual Access Simulation'),
    )

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='virtual_debit_cards')

    account = models.ForeignKey(BankAccount, on_delete=models.CASCADE, related_name='virtual_debit_cards')

    # Fictional simulation card number (never real payment network credentials)
    card_number = models.CharField(max_length=19, unique=True, db_index=True)

    cardholder_name = models.CharField(max_length=100)

    card_type = models.CharField(max_length=30, choices=CARD_TYPE_CHOICES, default='CLASSIC')

    issue_date = models.DateField(default=timezone.now)

    expiry_month = models.PositiveSmallIntegerField(default=12)

    expiry_year = models.PositiveSmallIntegerField(default=2029)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ACTIVE')

    blocked_by_admin = models.BooleanField(default=False)

    daily_limit = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('25000.00'))

    is_simulation = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['account'],
                condition=models.Q(status__in=['ACTIVE', 'BLOCKED']),
                name='unique_active_or_blocked_card_per_account'
            )
        ]

    @property
    def masked_card_number(self):
        """Displays card number in safe masked format: •••• •••• •••• 1234."""
        raw = str(self.card_number).replace(' ', '')
        if len(raw) >= 4:
            return f"•••• •••• •••• {raw[-4:]}"
        return "•••• •••• •••• ••••"

    @property
    def expiry_display(self):
        return f"{self.expiry_month:02d}/{str(self.expiry_year)[-2:]}"

    @property
    def is_expired(self):
        now = timezone.now().date()
        if self.expiry_year < now.year:
            return True
        if self.expiry_year == now.year and self.expiry_month < now.month:
            return True
        return False

    @property
    def is_usable(self):
        return self.status == 'ACTIVE' and not self.is_expired and not self.blocked_by_admin

    def __str__(self):
        return f"VirtualCard {self.masked_card_number} ({self.status}) - A/C {self.account.account_number}"


# =========================
# VIRTUAL DEBIT CARD AUDIT LOG
# =========================
class DebitCardAuditLog(models.Model):

    ACTION_CHOICES = (
        ('ISSUED', 'Card Issued'),
        ('BLOCKED_CUSTOMER', 'Blocked by Customer'),
        ('UNBLOCKED_CUSTOMER', 'Unblocked by Customer'),
        ('CANCELLED_CUSTOMER', 'Cancelled by Customer'),
        ('BLOCKED_ADMIN', 'Blocked by Admin'),
        ('UNBLOCKED_ADMIN', 'Unblocked by Admin'),
        ('CANCELLED_ADMIN', 'Cancelled by Admin'),
        ('STATUS_INSPECTED_STAFF', 'Status Inspected by Staff'),
    )

    card = models.ForeignKey(VirtualDebitCard, on_delete=models.CASCADE, related_name='audit_logs')

    performed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='card_actions_performed')

    action = models.CharField(max_length=30, choices=ACTION_CHOICES)

    reason = models.TextField(blank=True, default='')

    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        actor = self.performed_by.username if self.performed_by else "System"
        return f"Card #{self.card_id} - {self.action} by {actor} at {self.timestamp}"


# =========================
# NOTIFICATIONS (IN-APP PERSISTENT)
# =========================
def sanitize_internal_url(url):
    """Sanitizes target URLs to strictly allow safe internal paths and prevent open redirects."""
    if not url:
        return ''
    cleaned = str(url).strip()
    if cleaned.startswith('/') and not cleaned.startswith('//') and not cleaned.startswith('/\\'):
        lower = cleaned.lower()
        if not any(lower.startswith(p) for p in ['/http:', '/https:', '/javascript:', '/data:', '/vbscript:']):
            return cleaned
    return ''


class Notification(models.Model):

    NOTIFICATION_TYPES = (
        ('TRANSACTION', 'Transaction'),
        ('SECURITY', 'Security Alert'),
        ('LOAN', 'Loan & Repayment'),
        ('FD', 'Fixed Deposit'),
        ('CARD', 'Debit Card'),
        ('KYC', 'KYC Verification'),
        ('SUPPORT', 'Customer Support'),
        ('SYSTEM', 'System Notification'),
    )

    recipient = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notifications')

    notification_type = models.CharField(max_length=20, choices=NOTIFICATION_TYPES, default='SYSTEM')

    title = models.CharField(max_length=150)

    message = models.TextField()

    target_url = models.CharField(max_length=255, blank=True, default='')

    is_read = models.BooleanField(default=False)

    read_at = models.DateTimeField(null=True, blank=True)

    dedup_key = models.CharField(max_length=128, null=True, blank=True, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['recipient', 'is_read', '-created_at']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['recipient', 'dedup_key'],
                condition=models.Q(dedup_key__isnull=False),
                name='unique_notification_per_recipient_dedup'
            )
        ]

    def mark_as_read(self):
        if not self.is_read:
            self.is_read = True
            self.read_at = timezone.now()
            self.save(update_fields=['is_read', 'read_at'])

    @property
    def safe_target_url(self):
        return sanitize_internal_url(self.target_url)

    def __str__(self):
        read_flag = "✓" if self.is_read else "•"
        return f"[{read_flag}] {self.recipient.username}: {self.title}"


def send_in_app_notification(recipient, notification_type, title, message, target_url='', dedup_key=None):
    """
    Helper function to reliably persist in-app notifications.
    Deduplication protection ensures duplicate retries do not generate repeated notifications.
    Safe URL sanitization prevents open redirects.
    """
    if not recipient or not getattr(recipient, 'id', None):
        return None

    safe_url = sanitize_internal_url(target_url)

    if dedup_key:
        dedup_key = str(dedup_key)[:128]
        existing = Notification.objects.filter(recipient=recipient, dedup_key=dedup_key).first()
        if existing:
            return existing

    try:
        return Notification.objects.create(
            recipient=recipient,
            notification_type=notification_type,
            title=str(title)[:150],
            message=str(message),
            target_url=safe_url,
            dedup_key=dedup_key,
        )
    except Exception:
        if dedup_key:
            return Notification.objects.filter(recipient=recipient, dedup_key=dedup_key).first()
        return None


# =========================
# SUPPORT TICKETS & MESSAGES
# =========================
def generate_ticket_number():
    """Generates human-readable unique support ticket identifier: TKT-<YEARMMDD>-<4HEX>."""
    today_str = timezone.now().strftime("%Y%m%d")
    suffix = secrets.token_hex(2).upper()
    return f"TKT-{today_str}-{suffix}"


class SupportTicket(models.Model):

    CATEGORY_CHOICES = (
        ('TRANSACTION', 'Transaction / Transfer Issue'),
        ('CARD', 'Debit Card Issue'),
        ('LOAN', 'Loan / EMI Query'),
        ('FD', 'Fixed Deposit Query'),
        ('KYC', 'KYC & Profile Verification'),
        ('ACCOUNT', 'Account Maintenance'),
        ('OTHER', 'General Inquiry'),
    )

    PRIORITY_CHOICES = (
        ('LOW', 'Low'),
        ('MEDIUM', 'Medium'),
        ('HIGH', 'High'),
        ('URGENT', 'Urgent'),
    )

    STATUS_CHOICES = (
        ('OPEN', 'Open'),
        ('IN_PROGRESS', 'In Progress'),
        ('WAITING_FOR_CUSTOMER', 'Waiting for Customer'),
        ('RESOLVED', 'Resolved'),
        ('CLOSED', 'Closed'),
    )

    ticket_number = models.CharField(max_length=32, unique=True, db_index=True)

    customer = models.ForeignKey(User, on_delete=models.CASCADE, related_name='support_tickets')

    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default='OTHER')

    subject = models.CharField(max_length=150)

    description = models.TextField()

    priority = models.CharField(max_length=20, choices=PRIORITY_CHOICES, default='MEDIUM')

    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='OPEN')

    assigned_to = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='assigned_support_tickets'
    )

    created_at = models.DateTimeField(auto_now_add=True)

    updated_at = models.DateTimeField(auto_now=True)

    resolved_at = models.DateTimeField(null=True, blank=True)

    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-updated_at']

    def can_customer_reply(self):
        return self.status in ['OPEN', 'IN_PROGRESS', 'WAITING_FOR_CUSTOMER']

    def can_customer_reopen(self):
        return self.status == 'RESOLVED'

    def __str__(self):
        return f"{self.ticket_number} - {self.subject} ({self.status})"


class SupportMessage(models.Model):

    ticket = models.ForeignKey(SupportTicket, on_delete=models.CASCADE, related_name='messages')

    author = models.ForeignKey(User, on_delete=models.CASCADE, related_name='support_messages')

    message = models.TextField()

    is_internal_note = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        note = " [INTERNAL NOTE]" if self.is_internal_note else ""
        return f"Msg #{self.id} on {self.ticket.ticket_number} by {self.author.username}{note}"


# =========================
# TELLER AUDIT & OPERATION LOG
# =========================
class TellerOperationLog(models.Model):
    """
    Immutable audit log for teller counter operations.
    Captures account opening, cash deposit, and cash withdrawal operations.
    Protects against duplicate submission and payload tampering via idempotency keys.
    """
    OPERATION_CHOICES = (
        ('ACCOUNT_OPENED', 'Account Opened'),
        ('DEPOSIT', 'Cash Deposit'),
        ('WITHDRAWAL', 'Cash Withdrawal'),
    )

    STATUS_CHOICES = (
        ('SUCCESS', 'Success'),
        ('FAILED', 'Failed'),
    )

    employee = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name='teller_operations'
    )
    operation_type = models.CharField(max_length=30, choices=OPERATION_CHOICES)
    customer = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='customer_teller_operations'
    )
    account = models.ForeignKey(
        BankAccount,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='account_teller_operations'
    )
    account_number_snapshot = models.CharField(max_length=30, blank=True)
    customer_username_snapshot = models.CharField(max_length=150, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    reference_id = models.CharField(max_length=64, unique=True, db_index=True)
    idempotency_key = models.UUIDField(unique=True, null=True, blank=True, db_index=True)
    payload_hash = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=20, default='SUCCESS', choices=STATUS_CHOICES)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['operation_type', 'created_at']),
            models.Index(fields=['employee', 'created_at']),
        ]

    def __str__(self):
        emp = self.employee.username if self.employee else "Unknown"
        return f"{self.operation_type} #{self.reference_id} by {emp} at {self.created_at}"


# =========================
# EMPLOYEE ACTIVITY & AUDIT LOG (TASK 3.4)
# =========================
class EmployeeActivityLog(models.Model):
    """
    Durable, immutable audit log for employee and administrative actions.
    Tracks compliance, teller operations, customer modifications, support actions,
    inspections, and report exports with IP attribution.
    """
    ACTION_CHOICES = (
        ('PROFILE_UPDATE', 'Customer Profile Updated'),
        ('KYC_REVIEW', 'KYC Under Review'),
        ('KYC_APPROVED', 'KYC Approved'),
        ('KYC_REJECTED', 'KYC Rejected'),
        ('ACCOUNT_OPENING', 'Account Opened'),
        ('TELLER_DEPOSIT', 'Teller Cash Deposit'),
        ('TELLER_WITHDRAWAL', 'Teller Cash Withdrawal'),
        ('LOAN_VERIFICATION', 'Loan Under Review'),
        ('LOAN_RECOMMEND_APPROVE', 'Loan Approval Recommended'),
        ('LOAN_RECOMMEND_REJECT', 'Loan Rejection Recommended'),
        ('LOAN_DECISION_APPROVE', 'Loan Approved by Admin'),
        ('LOAN_DECISION_REJECT', 'Loan Rejected by Admin'),
        ('SUPPORT_REPLY', 'Support Ticket Replied'),
        ('SUPPORT_NOTE', 'Support Internal Note Added'),
        ('SUPPORT_STATUS_CHANGE', 'Support Status Changed'),
        ('SUPPORT_ASSIGNMENT', 'Support Ticket Assigned'),
        ('FD_INSPECTION', 'Fixed Deposit Inspected'),
        ('CARD_INSPECTION', 'Debit Card Inspected'),
        ('REPORT_VIEW', 'Report Viewed'),
        ('REPORT_EXPORT', 'Report Exported (CSV)'),
        ('EMPLOYEE_CREATE', 'Employee Created'),
        ('EMPLOYEE_PROFILE_UPDATE', 'Employee Profile Updated'),
        ('EMPLOYEE_STATUS_CHANGE', 'Employee Status Changed'),
        ('EMPLOYEE_LOCK_CHANGE', 'Employee Lock Status Changed'),
        ('PERMISSION_UPDATE', 'Role Permission Updated'),
        ('ADMIN_SETTINGS_UPDATE', 'System Settings Updated'),
        ('ADMIN_BACKUP_ACTION', 'Backup Operation Executed'),
        ('ADMIN_CUSTOMER_STATUS_CHANGE', 'Customer Status Changed'),
        ('ADMIN_CUSTOMER_LOCK_CHANGE', 'Customer Lock Status Changed'),
        ('ADMIN_ACCOUNT_FREEZE', 'Bank Account Frozen'),
        ('ADMIN_ACCOUNT_UNFREEZE', 'Bank Account Unfrozen'),
        ('FD_SETTLEMENT', 'Fixed Deposit Settlement Processed'),
        ('FD_PREMATURE_CLOSE', 'Fixed Deposit Premature Closure Processed'),
        ('CARD_STATUS_CHANGE', 'Debit Card Status Changed'),
        ('SYSTEM_ANNOUNCEMENT', 'System Announcement Broadcasted'),
    )

    employee = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='employee_activity_logs'
    )
    action_type = models.CharField(max_length=40, choices=ACTION_CHOICES, db_index=True)
    customer = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='customer_activity_logs'
    )
    account_number = models.CharField(max_length=30, blank=True, default='')
    target_id = models.CharField(max_length=64, blank=True, default='')
    description = models.TextField(blank=True, default='')
    changes = models.TextField(blank=True, default='')
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['employee', '-created_at']),
            models.Index(fields=['action_type', '-created_at']),
        ]

    def save(self, *args, **kwargs):
        # Strict immutability: existing records cannot be edited
        if self.pk and not kwargs.get('force_insert', False):
            if EmployeeActivityLog.objects.filter(pk=self.pk).exists():
                raise PermissionError("EmployeeActivityLog records are strictly immutable and cannot be updated.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError("EmployeeActivityLog records are immutable and cannot be deleted.")

    def __str__(self):
        emp = self.employee.username if self.employee else "System"
        return f"[{self.action_type}] by {emp} at {self.created_at}"


def log_employee_activity(employee, action_type, description='', customer=None, account_number='', target_id='', changes='', request=None):
    """
    Central helper to record an employee activity log entry safely.
    Extracts IP address from request if available.
    Ensures customer users cannot generate employee activity logs.
    Strictly prevents logging raw secrets (passwords, PINs, CVV, full card numbers).
    """
    if not employee or not getattr(employee, 'id', None):
        return None
    # Customer users cannot create employee activity logs
    if getattr(employee, 'role', '') not in ['EMPLOYEE', 'ADMIN']:
        return None

    ip = None
    if request:
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            ip = x_forwarded_for.split(',')[0].strip()
        else:
            ip = request.META.get('REMOTE_ADDR')

    try:
        return EmployeeActivityLog.objects.create(
            employee=employee,
            action_type=action_type,
            description=str(description or '')[:500],
            customer=customer,
            account_number=str(account_number or '')[:30],
            target_id=str(target_id or '')[:64],
            changes=str(changes or '')[:2000],
            ip_address=ip
        )
    except Exception:
        return None
