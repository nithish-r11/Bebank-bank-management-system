from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse, Http404, JsonResponse, HttpResponseForbidden
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator, PageNotAnInteger, EmptyPage
import logging
import uuid
from datetime import datetime, timedelta, date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from django.db.models import Sum, Q, Count
from django.db import transaction, DatabaseError, IntegrityError
from django.views.decorators.http import require_POST
from django.utils import timezone
from django.contrib import messages

logger = logging.getLogger(__name__)

from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet

import csv
import hashlib
import secrets
from accounts.models import User
from accounts.decorators import role_required
from .models import (
    BankAccount, Transaction, FixedDeposit, Loan, Beneficiary,
    LoanInstallment, LoanRepayment, add_months,
    VirtualDebitCard, DebitCardAuditLog, generate_simulation_card_number,
    Notification, SupportTicket, SupportMessage, send_in_app_notification, generate_ticket_number,
    TellerOperationLog, LoanVerificationLog,
    EmployeeActivityLog, log_employee_activity
)

# Dynamically derive maximum opening balance and precision from BankAccount metadata to prevent drift
_balance_field = BankAccount._meta.get_field('balance')
MAX_BALANCE = Decimal(
    '9' * (_balance_field.max_digits - _balance_field.decimal_places) +
    ('.' + '9' * _balance_field.decimal_places if _balance_field.decimal_places > 0 else '')
)
MAX_DECIMAL_PLACES = _balance_field.decimal_places


# =========================
# TELLER IDEMPOTENCY & AUDIT HELPERS
# =========================
def compute_teller_payload_hash(op_type, account_number, amount):
    """Compute a deterministic SHA-256 fingerprint of teller operation parameters."""
    raw = f"{str(op_type).strip().upper()}:{str(account_number).strip().upper()}:{Decimal(str(amount)):.2f}"
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def safe_csv_cell(val):
    """Sanitize spreadsheet cell to prevent formula injection (=, +, -, @, tab, cr)."""
    if val is None:
        return ""
    raw = str(val)
    stripped = raw.lstrip(' ')
    if stripped and stripped[0] in ('=', '+', '-', '@', '\t', '\r'):
        return f"'{raw.strip()}"
    return raw.strip()


# =========================
# CREATE ACCOUNT (UPGRADED FOR TASK 3.2)
# =========================
@role_required('EMPLOYEE', 'ADMIN')
def create_account_view(request):
    """
    Staff-managed account opening workflow.
    Supports creating an account for a brand new customer OR for an existing customer.
    Validates required contact information, opening balance, and uniqueness.
    Auditable via TellerOperationLog.
    """
    if request.method == "POST":
        customer_mode = request.POST.get("customer_mode", "new").strip().lower()
        existing_username = request.POST.get("existing_username", "").strip()

        account_type = request.POST.get("account_type", "").strip()
        balance_raw = request.POST.get("balance")

        email = request.POST.get("email", "").strip()
        phone = request.POST.get("phone", "").strip()
        aadhaar = request.POST.get("aadhaar", "").replace(" ", "").replace("-", "")
        pan = request.POST.get("pan", "").replace(" ", "").replace("-", "").upper()
        address = request.POST.get("address", "").strip()
        pincode = request.POST.get("pincode", "").strip()

        if account_type not in dict(BankAccount.ACCOUNT_TYPE):
            messages.error(request, "Invalid account type selected ❌")
            return redirect("create_account")

        # 🔴 OPENING BALANCE VALIDATION (Decimal-based)
        if balance_raw is None or str(balance_raw).strip() == "":
            messages.error(request, "Initial balance is required ❌")
            return redirect("create_account")

        try:
            balance = Decimal(str(balance_raw).strip())
        except (TypeError, ValueError, InvalidOperation):
            messages.error(request, "Invalid initial balance ❌")
            return redirect("create_account")

        if not balance.is_finite():
            messages.error(request, "Invalid initial balance ❌")
            return redirect("create_account")

        if balance < Decimal('0'):
            messages.error(request, "Initial balance cannot be negative ❌")
            return redirect("create_account")

        if balance.as_tuple().exponent < -MAX_DECIMAL_PLACES:
            messages.error(request, f"Initial balance cannot have more than {MAX_DECIMAL_PLACES} decimal places ❌")
            return redirect("create_account")

        if balance > MAX_BALANCE:
            messages.error(request, "Initial balance exceeds maximum allowed limit ❌")
            return redirect("create_account")

        user = None

        if customer_mode == "existing" or (existing_username and not request.POST.get("username")):
            # Existing Customer Mode
            user = User.objects.filter(username=existing_username, role='CUSTOMER').first()
            if not user:
                messages.error(request, f"Customer '{existing_username}' not found or not eligible ❌")
                return redirect("create_account")

            if user.account_locked or not user.is_active:
                messages.error(request, "Cannot open an account for a locked or inactive customer ❌")
                return redirect("create_account")

            # Fallback to existing user contact details if empty
            email = email or user.email
            phone = phone or user.phone
            aadhaar = aadhaar or user.aadhaar
            pan = pan or user.pan
            address = address or user.address
            pincode = pincode or user.pincode

        else:
            # New Customer Mode
            username = request.POST.get("username", "").strip()
            password = request.POST.get("password", "")

            if not username:
                messages.error(request, "Username is required ❌")
                return redirect("create_account")

            if not password:
                messages.error(request, "Password is required ❌")
                return redirect("create_account")

            if User.objects.filter(username=username).exists():
                messages.error(request, "Username already exists ❌")
                return redirect("create_account")

            if len(aadhaar) != 12 or not aadhaar.isdigit():
                messages.error(request, "Aadhaar must be 12 digits ❌")
                return redirect("create_account")

            if len(pan) != 10:
                messages.error(request, "PAN must be 10 characters ❌")
                return redirect("create_account")

            if len(pincode) != 6:
                messages.error(request, "Pincode must be 6 digits ❌")
                return redirect("create_account")

        # 🔴 ATOMIC USER + BANKACCOUNT CREATION (Rolls back User on BankAccount failure)
        try:
            with transaction.atomic():
                if not user:
                    user = User.objects.create_user(
                        username=username,
                        password=password,
                        role="CUSTOMER"
                    )

                # Collision-safe account number generation
                last_acc = BankAccount.objects.select_for_update().order_by('-id').first()
                if last_acc and last_acc.account_number.isdigit():
                    next_num = str(int(last_acc.account_number) + 1)
                else:
                    next_num = "10001"

                while BankAccount.objects.filter(account_number=next_num).exists():
                    next_num = str(int(next_num) + 1)

                account = BankAccount.objects.create(
                    user=user,
                    account_number=next_num,
                    account_type=account_type,
                    balance=balance,
                    email=email,
                    phone=phone,
                    aadhaar=aadhaar,
                    pan=pan,
                    address=address,
                    pincode=pincode
                )

                if balance > Decimal('0.00'):
                    Transaction.objects.create(
                        account=account,
                        transaction_type="DEPOSIT",
                        amount=balance,
                        balance_before=Decimal('0.00'),
                        balance_after=balance,
                        reference_id=f"DEP-INIT-{secrets.token_hex(4).upper()}"
                    )

                # Record in TellerOperationLog
                ref_id = f"TLR-OPN-{secrets.token_hex(6).upper()}"
                TellerOperationLog.objects.create(
                    employee=request.user,
                    operation_type='ACCOUNT_OPENED',
                    customer=user,
                    account=account,
                    account_number_snapshot=account.account_number,
                    customer_username_snapshot=user.username,
                    amount=balance,
                    reference_id=ref_id,
                    status='SUCCESS',
                    description=f"Opened {account.account_type} account {account.account_number} for customer {user.username} with opening balance ₹{balance:,.2f}"
                )
                log_employee_activity(
                    employee=request.user,
                    action_type='ACCOUNT_OPENING',
                    customer=user,
                    account_number=account.account_number,
                    target_id=ref_id,
                    description=f"Opened {account.account_type} account {account.account_number} for customer {user.username} with balance ₹{balance:,.2f}",
                    request=request
                )

        except (DatabaseError, ValidationError):
            logger.exception("Account creation failed during database transaction.")
            messages.error(request, "Account creation failed due to a server error. Please try again ❌")
            return redirect("create_account")

        # Post-commit in-app notification
        send_in_app_notification(
            recipient=user,
            notification_type='ACCOUNT',
            title='Bank Account Opened',
            message=f"Welcome to BEBANK! Your new {account.account_type} account {account.account_number} has been created with balance ₹{balance:,.2f}.",
            target_url='/customer/dashboard/'
        )

        messages.success(request, f"Account {account.account_number} Created Successfully ✅")
        return render(request, "account_details.html", {"account": account})

    # GET request
    existing_username = request.GET.get("customer", "").strip()
    existing_customer = None
    if existing_username:
        existing_customer = User.objects.filter(username=existing_username, role='CUSTOMER').first()

    return render(request, "create_account.html", {
        "existing_customer": existing_customer,
        "existing_username": existing_username
    })


# =========================
# WITHDRAW
# =========================
@role_required('CUSTOMER')
def withdraw_view(request):

    account_id = request.POST.get('account_id') or request.GET.get('account_id')
    if account_id:
        account = get_object_or_404(BankAccount, id=account_id, user=request.user)
    else:
        account = BankAccount.objects.filter(user=request.user).last()

    if request.method == 'POST':

        try:
            amount = Decimal(request.POST['amount'])
        except (KeyError, ValueError, InvalidOperation):
            messages.error(request, "Invalid amount ❌")
            return redirect("withdraw")

        if not account:
            messages.error(request, "Account not found ❌")
            return redirect("withdraw")

        if amount <= 0:
            messages.error(request, "Enter valid amount ❌")
            return redirect("withdraw")

        with transaction.atomic():
            # Acquire row lock and re-validate balance inside the atomic transaction
            locked_account = get_object_or_404(
                BankAccount.objects.select_for_update(),
                id=account.id,
                user=request.user
            )

            if locked_account.is_frozen:
                messages.error(request, "This bank account is frozen. Withdrawals are prohibited ❌")
                return redirect("withdraw")

            if locked_account.balance < amount:
                messages.error(request, "Insufficient balance ❌")
                return redirect("withdraw")

            locked_account.balance -= amount
            locked_account.save(update_fields=['balance'])

            Transaction.objects.create(
                account=locked_account,
                transaction_type="WITHDRAW",
                amount=amount
            )

        send_in_app_notification(
            recipient=request.user,
            notification_type='TRANSACTION',
            title="Withdrawal Successful",
            message=f"₹{amount:,.2f} was withdrawn from account {locked_account.account_number}.",
            target_url="/bank/history/"
        )

        messages.success(request, "Withdrawal successful ✅")
        return redirect("withdraw")

    return render(request, 'withdraw.html', {'account': account})


# =========================
# TRANSFER
# =========================
@role_required('CUSTOMER')
def transfer_view(request):

    account_id = request.POST.get('account_id') or request.POST.get('source_account_id') or request.GET.get('account_id')
    if account_id:
        sender = get_object_or_404(BankAccount, id=account_id, user=request.user)
    else:
        sender = BankAccount.objects.filter(user=request.user).last()

    if request.method == 'POST':

        # ⭐ Backend Idempotency Validation
        raw_key = request.POST.get('idempotency_key', '').strip()
        if not raw_key:
            messages.error(request, "Missing transaction idempotency key ❌")
            return redirect("transfer")

        try:
            key_uuid = uuid.UUID(raw_key)
        except (ValueError, AttributeError):
            messages.error(request, "Malformed transaction idempotency key ❌")
            return redirect("transfer")

        # Full 128-bit UUID entropy in reference ID
        reference_id = f"TRF-{key_uuid.hex.upper()}"

        beneficiary_id = request.POST.get('beneficiary_id', '').strip()
        acc_no = request.POST.get('account_number', '').strip() if request.POST.get('account_number') else ''
        amount = request.POST.get('amount')

        if not sender:
            messages.error(request, "Your account not found ❌")
            return redirect("transfer")

        try:
            amount = Decimal(amount)
        except (TypeError, ValueError, InvalidOperation):
            messages.error(request, "Invalid amount ❌")
            return redirect("transfer")

        if amount <= 0:
            messages.error(request, "Enter valid amount ❌")
            return redirect("transfer")

        if beneficiary_id:
            # Revalidate beneficiary ownership (IDOR protection)
            try:
                beneficiary = Beneficiary.objects.select_related('account', 'account__user').get(
                    id=beneficiary_id,
                    user=request.user
                )
            except (Beneficiary.DoesNotExist, ValueError):
                messages.error(request, "Selected beneficiary not found or unauthorized ❌")
                return redirect("transfer")

            # Verify destination account active status
            if not beneficiary.is_available():
                messages.error(request, "Selected beneficiary is currently unavailable or inactive ❌")
                return redirect("transfer")

            receiver = beneficiary.account
            if acc_no and acc_no != receiver.account_number:
                messages.error(request, "Beneficiary account mismatch ❌")
                return redirect("transfer")
            acc_no = receiver.account_number
        else:
            if not acc_no:
                messages.error(request, "Please enter recipient account number or select a beneficiary ❌")
                return redirect("transfer")

            receiver = BankAccount.objects.filter(account_number=acc_no).first()

            if not receiver:
                messages.error(request, "Invalid account number ❌")
                return redirect("transfer")

        if sender.id == receiver.id or sender.account_number == receiver.account_number:
            messages.error(request, "Cannot transfer to the same account ❌")
            return redirect("transfer")

        # ⭐ Check existing transfer records before processing retries
        existing_debit = Transaction.objects.filter(
            reference_id=reference_id,
            transaction_type='TRANSFER_DEBIT'
        ).select_related('account', 'related_account').first()

        if existing_debit:
            is_same_sender = (existing_debit.account_id == sender.id)
            is_same_receiver = (
                existing_debit.related_account and
                existing_debit.related_account.account_number == acc_no
            )
            is_same_amount = (existing_debit.amount == amount)

            if is_same_sender and is_same_receiver and is_same_amount:
                messages.info(request, "Transfer already processed successfully ✅")
                return redirect("transfer")
            else:
                messages.error(request, "Idempotency key has already been used for a different transfer ❌")
                return redirect("transfer")

        try:
            with transaction.atomic():
                # Deadlock prevention: sort account IDs to acquire locks in consistent order
                ordered_ids = sorted([sender.id, receiver.id])
                locked_map = {
                    acc.id: acc
                    for acc in BankAccount.objects.select_for_update().filter(id__in=ordered_ids).order_by('id')
                }

                locked_sender = locked_map.get(sender.id)
                locked_receiver = locked_map.get(receiver.id)

                if not locked_sender or locked_sender.user_id != request.user.id:
                    raise Http404("Source account not found or access denied.")

                if not locked_receiver:
                    messages.error(request, "Invalid account number ❌")
                    return redirect("transfer")

                # Financial safety: reject locked, inactive, or frozen accounts
                if locked_sender.user.account_locked or not locked_sender.user.is_active:
                    messages.error(request, "Your account is locked or inactive ❌")
                    return redirect("transfer")

                if locked_sender.is_frozen:
                    messages.error(request, "Your bank account is frozen and cannot initiate transfers ❌")
                    return redirect("transfer")

                if locked_receiver.user.account_locked or not locked_receiver.user.is_active:
                    messages.error(request, "Recipient account is locked or inactive ❌")
                    return redirect("transfer")

                # Re-verify under row lock to prevent race condition
                concurrent_debit = Transaction.objects.filter(
                    reference_id=reference_id,
                    transaction_type='TRANSFER_DEBIT'
                ).select_related('account', 'related_account').first()

                if concurrent_debit:
                    if (concurrent_debit.account_id == locked_sender.id and
                        concurrent_debit.related_account_id == locked_receiver.id and
                        concurrent_debit.amount == amount):
                        messages.info(request, "Transfer already processed successfully ✅")
                        return redirect("transfer")
                    else:
                        messages.error(request, "Idempotency key has already been used for a different transfer ❌")
                        return redirect("transfer")

                if locked_sender.balance < amount:
                    messages.error(request, "Insufficient balance ❌")
                    return redirect("transfer")

                sender_before = locked_sender.balance
                receiver_before = locked_receiver.balance

                sender_after = sender_before - amount
                receiver_after = receiver_before + amount

                locked_sender.balance = sender_after
                locked_receiver.balance = receiver_after

                locked_sender.save(update_fields=['balance'])
                locked_receiver.save(update_fields=['balance'])

                # Leg 1: Sender Debit
                Transaction.objects.create(
                    account=locked_sender,
                    related_account=locked_receiver,
                    transaction_type="TRANSFER_DEBIT",
                    amount=amount,
                    balance_before=sender_before,
                    balance_after=sender_after,
                    reference_id=reference_id
                )

                # Leg 2: Recipient Credit
                Transaction.objects.create(
                    account=locked_receiver,
                    related_account=locked_sender,
                    transaction_type="TRANSFER_CREDIT",
                    amount=amount,
                    balance_before=receiver_before,
                    balance_after=receiver_after,
                    reference_id=reference_id
                )

        except IntegrityError:
            # Handle concurrent race condition outside the failed atomic block
            confirmed_tx = Transaction.objects.filter(
                reference_id=reference_id,
                transaction_type='TRANSFER_DEBIT'
            ).first()
            if confirmed_tx and confirmed_tx.account_id == sender.id and confirmed_tx.amount == amount:
                messages.info(request, "Transfer already processed successfully ✅")
                return redirect("transfer")
            messages.error(request, "Transfer conflict or duplicate processing error ❌")
            return redirect("transfer")

        if sender and sender.user:
            send_in_app_notification(
                recipient=sender.user,
                notification_type='TRANSACTION',
                title="Transfer Sent",
                message=f"₹{amount:,.2f} transferred to account {receiver.account_number}. Reference: {reference_id}",
                target_url="/bank/history/",
                dedup_key=f"TRF-DEBIT-NOTIF-{reference_id}"
            )
        if receiver and receiver.user:
            send_in_app_notification(
                recipient=receiver.user,
                notification_type='TRANSACTION',
                title="Transfer Received",
                message=f"₹{amount:,.2f} received from account {sender.account_number}. Reference: {reference_id}",
                target_url="/bank/history/",
                dedup_key=f"TRF-CREDIT-NOTIF-{reference_id}"
            )

        messages.success(request, "Transfer successful ✅")
        return redirect("transfer")

    idempotency_key = str(uuid.uuid4())
    beneficiaries = Beneficiary.objects.filter(
        user=request.user
    ).select_related('account', 'account__user').order_by('nickname', '-created_at')
    selected_beneficiary_id = request.GET.get('beneficiary_id', '').strip()

    return render(request, "transfer.html", {
        'idempotency_key': idempotency_key,
        'beneficiaries': beneficiaries,
        'selected_beneficiary_id': selected_beneficiary_id,
    })


# =========================
# BENEFICIARY MANAGEMENT
# =========================
@role_required('CUSTOMER')
def beneficiary_list_view(request):
    if request.method == 'POST':
        account_number = request.POST.get('account_number', '').strip()
        nickname = request.POST.get('nickname', '').strip()

        if not account_number:
            messages.error(request, "Please enter an account number ❌")
            return redirect("beneficiary_list")

        # Destination account lookup
        target_account = BankAccount.objects.filter(account_number=account_number).select_related('user').first()

        # Check existence and active/unlocked status
        if not target_account or not target_account.user.is_active or target_account.user.account_locked:
            messages.error(request, "Destination account not found or unavailable ❌")
            return redirect("beneficiary_list")

        # Prevent adding own account
        if target_account.user_id == request.user.id:
            messages.error(request, "You cannot add your own account as a beneficiary ❌")
            return redirect("beneficiary_list")

        # Prevent duplicate beneficiaries
        if Beneficiary.objects.filter(user=request.user, account=target_account).exists():
            messages.error(request, "This account is already in your beneficiary list ❌")
            return redirect("beneficiary_list")

        try:
            Beneficiary.objects.create(
                user=request.user,
                account=target_account,
                nickname=nickname[:50]
            )
        except IntegrityError:
            messages.error(request, "This account is already in your beneficiary list ❌")
            return redirect("beneficiary_list")

        messages.success(request, f"Beneficiary for account {target_account.account_number} added successfully ✅")
        return redirect("beneficiary_list")

    beneficiaries = Beneficiary.objects.filter(
        user=request.user
    ).select_related('account', 'account__user').order_by('-created_at')

    return render(request, "beneficiary_list.html", {'beneficiaries': beneficiaries})


@require_POST
@role_required('CUSTOMER')
def beneficiary_edit_view(request, beneficiary_id):
    beneficiary = get_object_or_404(Beneficiary, id=beneficiary_id, user=request.user)
    nickname = request.POST.get('nickname', '').strip()
    beneficiary.nickname = nickname[:50]
    beneficiary.save(update_fields=['nickname'])
    messages.success(request, "Beneficiary nickname updated successfully ✅")
    return redirect("beneficiary_list")


@require_POST
@role_required('CUSTOMER')
def beneficiary_delete_view(request, beneficiary_id):
    beneficiary = get_object_or_404(Beneficiary, id=beneficiary_id, user=request.user)
    beneficiary.delete()
    messages.success(request, "Beneficiary removed successfully ✅")
    return redirect("beneficiary_list")


# =========================
# TELLER DEPOSIT WORKFLOW (UPGRADED FOR TASK 3.2)
# =========================
@role_required('EMPLOYEE', 'ADMIN')
def staff_deposit_view(request):
    """
    Dedicated teller cash deposit workflow.
    Validates account, active status, and positive Decimal amount.
    Protected by UUID idempotency token against duplicate submission or payload alteration.
    Credits balance atomically, records DEPOSIT transaction and TellerOperationLog, and sends notification.
    """
    account_number = request.GET.get('account_number', '').strip() or request.POST.get('account_number', '').strip()
    account = None
    if account_number:
        account = BankAccount.objects.filter(account_number=account_number).first()

    if request.method == "POST":
        acc_no = request.POST.get("account_number", "").strip()
        amount_raw = request.POST.get("amount", "").strip()
        notes = request.POST.get("notes", "").strip()
        idempotency_raw = request.POST.get("idempotency_key", "").strip()

        # Idempotency token validation
        idempotency_key = None
        if idempotency_raw:
            try:
                idempotency_key = uuid.UUID(idempotency_raw)
            except (ValueError, AttributeError):
                messages.error(request, "Invalid idempotency key format ❌")
                return redirect("staff_deposit")

        account = BankAccount.objects.filter(account_number=acc_no).first()
        if not account:
            messages.error(request, "Account not found ❌")
            return redirect("staff_deposit")

        if account.user and (account.user.account_locked or not account.user.is_active):
            messages.error(request, "Cannot deposit into a locked or inactive customer account ❌")
            return redirect("staff_deposit")

        try:
            amount = Decimal(amount_raw)
        except (TypeError, ValueError, InvalidOperation):
            messages.error(request, "Invalid amount entered ❌")
            return redirect("staff_deposit")

        if not amount.is_finite() or amount <= Decimal('0.00'):
            messages.error(request, "Amount must be greater than zero ❌")
            return redirect("staff_deposit")

        if amount > MAX_BALANCE:
            messages.error(request, "Deposit amount exceeds maximum system limit ❌")
            return redirect("staff_deposit")

        # Idempotency duplicate and payload tampering protection
        payload_hash = compute_teller_payload_hash('DEPOSIT', account.account_number, amount)
        if idempotency_key:
            existing_op = TellerOperationLog.objects.filter(idempotency_key=idempotency_key).first()
            if existing_op:
                if existing_op.payload_hash == payload_hash:
                    messages.info(request, "Transaction already processed (Idempotent replay) ✅")
                    return redirect('staff_teller_receipt', reference_id=existing_op.reference_id)
                else:
                    messages.error(request, "Idempotency token reuse detected with altered parameters ❌")
                    return HttpResponse("Idempotency token reuse detected with altered parameters.", status=400)

        ref_id = f"TLR-DEP-{secrets.token_hex(6).upper()}"

        with transaction.atomic():
            locked_account = get_object_or_404(
                BankAccount.objects.select_for_update(),
                id=account.id
            )
            bal_before = locked_account.balance
            locked_account.balance += amount
            bal_after = locked_account.balance
            locked_account.save(update_fields=['balance'])

            Transaction.objects.create(
                account=locked_account,
                transaction_type="DEPOSIT",
                amount=amount,
                balance_before=bal_before,
                balance_after=bal_after,
                reference_id=ref_id
            )

            desc = f"Cash deposit of ₹{amount:,.2f} into account {locked_account.account_number}"
            if notes:
                desc += f" (Note: {notes})"

            TellerOperationLog.objects.create(
                employee=request.user,
                operation_type="DEPOSIT",
                customer=locked_account.user,
                account=locked_account,
                account_number_snapshot=locked_account.account_number,
                customer_username_snapshot=locked_account.user.username if locked_account.user else '',
                amount=amount,
                reference_id=ref_id,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                status="SUCCESS",
                description=desc
            )
            log_employee_activity(
                employee=request.user,
                action_type='TELLER_DEPOSIT',
                customer=locked_account.user,
                account_number=locked_account.account_number,
                target_id=ref_id,
                description=desc,
                request=request
            )

        if locked_account.user:
            send_in_app_notification(
                recipient=locked_account.user,
                notification_type='TRANSACTION',
                title="Deposit Received",
                message=f"₹{amount:,.2f} deposited into account {locked_account.account_number} (Ref: {ref_id}). New balance: ₹{bal_after:,.2f}.",
                target_url="/bank/history/"
            )

        messages.success(request, f"Deposit of ₹{amount:,.2f} into account {locked_account.account_number} successful ✅")
        return redirect('staff_teller_receipt', reference_id=ref_id)

    # GET request
    fresh_idempotency_key = uuid.uuid4()
    return render(request, "staff_deposit.html", {
        "account": account,
        "account_number": account_number,
        "idempotency_key": fresh_idempotency_key,
    })


# Support both URL names for backwards compatibility
deposit_view = staff_deposit_view


# =========================
# TELLER WITHDRAWAL WORKFLOW (UPGRADED FOR TASK 3.2)
# =========================
@role_required('EMPLOYEE', 'ADMIN')
def staff_withdraw_view(request):
    """
    Dedicated teller cash withdrawal workflow.
    Validates customer active/locked status, ownership, and sufficient balance.
    Protected by UUID idempotency token against duplicate submission or payload alteration.
    Debits balance atomically using row locks, records WITHDRAW transaction and TellerOperationLog, and sends notification.
    """
    account_number = request.GET.get('account_number', '').strip() or request.POST.get('account_number', '').strip()
    account = None
    if account_number:
        account = BankAccount.objects.filter(account_number=account_number).first()

    if request.method == "POST":
        acc_no = request.POST.get("account_number", "").strip()
        amount_raw = request.POST.get("amount", "").strip()
        notes = request.POST.get("notes", "").strip()
        idempotency_raw = request.POST.get("idempotency_key", "").strip()

        # Idempotency token validation
        idempotency_key = None
        if idempotency_raw:
            try:
                idempotency_key = uuid.UUID(idempotency_raw)
            except (ValueError, AttributeError):
                messages.error(request, "Invalid idempotency key format ❌")
                return redirect("staff_withdraw")

        account = BankAccount.objects.filter(account_number=acc_no).first()
        if not account:
            messages.error(request, "Account not found ❌")
            return redirect("staff_withdraw")

        if account.user and (account.user.account_locked or not account.user.is_active):
            messages.error(request, "Cannot withdraw from a locked or inactive customer account ❌")
            return redirect("staff_withdraw")

        if account.is_frozen:
            messages.error(request, "Account is frozen. Counter cash withdrawals are prohibited ❌")
            return redirect("staff_withdraw")

        try:
            amount = Decimal(amount_raw)
        except (TypeError, ValueError, InvalidOperation):
            messages.error(request, "Invalid amount entered ❌")
            return redirect("staff_withdraw")

        if not amount.is_finite() or amount <= Decimal('0.00'):
            messages.error(request, "Amount must be greater than zero ❌")
            return redirect("staff_withdraw")

        # Idempotency duplicate and payload tampering protection
        payload_hash = compute_teller_payload_hash('WITHDRAWAL', account.account_number, amount)
        if idempotency_key:
            existing_op = TellerOperationLog.objects.filter(idempotency_key=idempotency_key).first()
            if existing_op:
                if existing_op.payload_hash == payload_hash:
                    messages.info(request, "Transaction already processed (Idempotent replay) ✅")
                    return redirect('staff_teller_receipt', reference_id=existing_op.reference_id)
                else:
                    messages.error(request, "Idempotency token reuse detected with altered parameters ❌")
                    return HttpResponse("Idempotency token reuse detected with altered parameters.", status=400)

        ref_id = f"TLR-WTH-{secrets.token_hex(6).upper()}"

        with transaction.atomic():
            locked_account = get_object_or_404(
                BankAccount.objects.select_for_update(),
                id=account.id
            )
            if locked_account.is_frozen:
                messages.error(request, "Account is frozen. Counter cash withdrawals are prohibited ❌")
                return redirect("staff_withdraw")

            if locked_account.balance < amount:
                messages.error(request, f"Insufficient funds. Available balance: ₹{locked_account.balance:,.2f} ❌")
                return redirect("staff_withdraw")

            bal_before = locked_account.balance
            locked_account.balance -= amount
            bal_after = locked_account.balance
            locked_account.save(update_fields=['balance'])

            Transaction.objects.create(
                account=locked_account,
                transaction_type="WITHDRAW",
                amount=amount,
                balance_before=bal_before,
                balance_after=bal_after,
                reference_id=ref_id
            )

            desc = f"Cash withdrawal of ₹{amount:,.2f} from account {locked_account.account_number}"
            if notes:
                desc += f" (Note: {notes})"

            TellerOperationLog.objects.create(
                employee=request.user,
                operation_type="WITHDRAWAL",
                customer=locked_account.user,
                account=locked_account,
                account_number_snapshot=locked_account.account_number,
                customer_username_snapshot=locked_account.user.username if locked_account.user else '',
                amount=amount,
                reference_id=ref_id,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                status="SUCCESS",
                description=desc
            )
            log_employee_activity(
                employee=request.user,
                action_type='TELLER_WITHDRAWAL',
                customer=locked_account.user,
                account_number=locked_account.account_number,
                target_id=ref_id,
                description=desc,
                request=request
            )

        if locked_account.user:
            send_in_app_notification(
                recipient=locked_account.user,
                notification_type='TRANSACTION',
                title="In-Branch Cash Withdrawal",
                message=f"₹{amount:,.2f} has been withdrawn from account {locked_account.account_number} at branch counter (Ref: {ref_id}). New balance: ₹{bal_after:,.2f}.",
                target_url="/bank/history/"
            )

        messages.success(request, f"Withdrawal of ₹{amount:,.2f} from account {locked_account.account_number} successful ✅")
        return redirect('staff_teller_receipt', reference_id=ref_id)

    # GET request
    fresh_idempotency_key = uuid.uuid4()
    return render(request, "staff_withdraw.html", {
        "account": account,
        "account_number": account_number,
        "idempotency_key": fresh_idempotency_key,
    })


# =========================
# TELLER OPERATIONS DASHBOARD (UPGRADED FOR TASK 3.2)
# =========================
@role_required('EMPLOYEE', 'ADMIN')
def staff_operations_view(request):
    """
    Branch Teller Operations Hub.
    Real database metrics for today's account openings, deposits, withdrawals,
    total transaction value, recent operations, and pending operational tasks.
    """
    today = timezone.localdate()

    # Today's account openings
    today_openings = BankAccount.objects.filter(created_at__date=today).count()

    # Today's teller deposits
    today_deposits_agg = Transaction.objects.filter(
        transaction_type='DEPOSIT',
        created_at__date=today
    ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
    today_deposits_count = Transaction.objects.filter(
        transaction_type='DEPOSIT',
        created_at__date=today
    ).count()

    # Today's teller withdrawals
    today_withdrawals_agg = Transaction.objects.filter(
        transaction_type='WITHDRAW',
        created_at__date=today
    ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
    today_withdrawals_count = Transaction.objects.filter(
        transaction_type='WITHDRAW',
        created_at__date=today
    ).count()

    # Today's total teller transaction value
    today_total_volume = today_deposits_agg + today_withdrawals_agg

    # Recent teller operations
    recent_operations = TellerOperationLog.objects.select_related(
        'employee', 'customer', 'account'
    ).order_by('-created_at')[:8]

    # Pending operational items
    pending_kyc = User.objects.filter(role='CUSTOMER', kyc_status='PENDING').count()
    pending_tickets = SupportTicket.objects.filter(status__in=['OPEN', 'IN_PROGRESS']).count()
    total_customers = User.objects.filter(role='CUSTOMER').count()
    total_accounts = BankAccount.objects.count()

    return render(request, "staff_operations.html", {
        'today_openings': today_openings,
        'today_deposits': today_deposits_agg,
        'today_deposits_count': today_deposits_count,
        'today_withdrawals': today_withdrawals_agg,
        'today_withdrawals_count': today_withdrawals_count,
        'today_total_volume': today_total_volume,
        'recent_operations': recent_operations,
        'pending_kyc': pending_kyc,
        'pending_tickets': pending_tickets,
        'total_customers': total_customers,
        'total_accounts': total_accounts,
    })


# =========================
# TELLER TRANSACTION HISTORY
# =========================
@role_required('EMPLOYEE', 'ADMIN')
def staff_teller_history_view(request):
    """
    Searchable and filterable history of teller operations.
    Supports date range filtering, operation type, search query, and pagination.
    """
    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()
    op_type = request.GET.get('op_type', '').strip().upper()
    q = request.GET.get('q', '').strip()

    qs = TellerOperationLog.objects.select_related('employee', 'customer', 'account').order_by('-created_at')

    # Date filters
    if date_from:
        try:
            d_from = datetime.strptime(date_from, '%Y-%m-%d').date()
            qs = qs.filter(created_at__date__gte=d_from)
        except ValueError:
            pass

    if date_to:
        try:
            d_to = datetime.strptime(date_to, '%Y-%m-%d').date()
            qs = qs.filter(created_at__date__lte=d_to)
        except ValueError:
            pass

    # Operation type filter
    if op_type in ('ACCOUNT_OPENED', 'DEPOSIT', 'WITHDRAWAL'):
        qs = qs.filter(operation_type=op_type)
    else:
        op_type = 'ALL'

    # Search query
    if q:
        qs = qs.filter(
            Q(reference_id__icontains=q) |
            Q(account_number_snapshot__icontains=q) |
            Q(customer_username_snapshot__icontains=q) |
            Q(description__icontains=q)
        ).distinct()

    total_count = qs.count()
    total_volume = qs.filter(amount__isnull=False).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

    paginator = Paginator(qs, 20)
    page_number = request.GET.get('page', 1)
    try:
        page_obj = paginator.page(page_number)
    except (PageNotAnInteger, EmptyPage):
        page_obj = paginator.page(1)

    return render(request, "staff_teller_history.html", {
        'page_obj': page_obj,
        'date_from': date_from,
        'date_to': date_to,
        'op_type': op_type,
        'q': q,
        'total_count': total_count,
        'total_volume': total_volume,
        'operation_choices': TellerOperationLog.OPERATION_CHOICES,
    })


# =========================
# TELLER TRANSACTION RECEIPT
# =========================
@role_required('EMPLOYEE', 'ADMIN')
def staff_teller_receipt_view(request, reference_id):
    """
    Printable teller transaction receipt for staff and customers.
    """
    op_log = get_object_or_404(TellerOperationLog, reference_id=reference_id)
    return render(request, "staff_teller_receipt.html", {
        "op_log": op_log
    })


# =========================
# TELLER REPORT CSV EXPORT
# =========================
@role_required('EMPLOYEE', 'ADMIN')
def staff_teller_export_csv_view(request):
    """
    Export teller operation logs to CSV with safe formula escaping to prevent CSV injection.
    """
    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()
    op_type = request.GET.get('op_type', '').strip().upper()
    q = request.GET.get('q', '').strip()

    qs = TellerOperationLog.objects.select_related('employee', 'customer', 'account').order_by('-created_at')

    if date_from:
        try:
            d_from = datetime.strptime(date_from, '%Y-%m-%d').date()
            qs = qs.filter(created_at__date__gte=d_from)
        except ValueError:
            pass

    if date_to:
        try:
            d_to = datetime.strptime(date_to, '%Y-%m-%d').date()
            qs = qs.filter(created_at__date__lte=d_to)
        except ValueError:
            pass

    if op_type in ('ACCOUNT_OPENED', 'DEPOSIT', 'WITHDRAWAL'):
        qs = qs.filter(operation_type=op_type)

    if q:
        qs = qs.filter(
            Q(reference_id__icontains=q) |
            Q(account_number_snapshot__icontains=q) |
            Q(customer_username_snapshot__icontains=q) |
            Q(description__icontains=q)
        ).distinct()

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    filename = f"bebank_teller_report_{timezone.localdate().strftime('%Y%m%d')}.csv"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'

    writer = csv.writer(response)
    writer.writerow([
        'Reference ID',
        'Timestamp (UTC)',
        'Operation Type',
        'Officer Username',
        'Customer Username',
        'Account Number',
        'Amount (INR)',
        'Status',
        'Description'
    ])

    for row in qs[:1000]:
        writer.writerow([
            safe_csv_cell(row.reference_id),
            safe_csv_cell(row.created_at.strftime('%Y-%m-%d %H:%M:%S')),
            safe_csv_cell(row.get_operation_type_display()),
            safe_csv_cell(row.employee.username if row.employee else 'System'),
            safe_csv_cell(row.customer_username_snapshot or (row.customer.username if row.customer else '-')),
            safe_csv_cell(row.account_number_snapshot or (row.account.account_number if row.account else '-')),
            safe_csv_cell(f"{row.amount:.2f}" if row.amount is not None else "-"),
            safe_csv_cell(row.status),
            safe_csv_cell(row.description),
        ])

    return response


MAX_PDF_RECORDS = 500


# =========================
# HISTORY
# =========================
@role_required('CUSTOMER')
def history_view(request, account_id=None):

    account_id = account_id or request.GET.get('account_id') or request.POST.get('account_id')
    if account_id:
        account = get_object_or_404(BankAccount, id=account_id, user=request.user)
    else:
        account = BankAccount.objects.filter(user=request.user).last()

    if not account:
        txns = Transaction.objects.none()
    else:
        txns = Transaction.objects.filter(account=account, account__user=request.user).order_by('-created_at')

    # Filters
    selected_type = (request.GET.get('type') or request.GET.get('transaction_type') or '').strip().upper()
    selected_flow = (request.GET.get('flow') or request.GET.get('direction') or '').strip().lower()
    date_from_raw = (request.GET.get('date_from') or request.GET.get('from') or '').strip()
    date_to_raw = (request.GET.get('date_to') or request.GET.get('to') or '').strip()

    valid_types = [t[0] for t in Transaction.TRANSACTION_TYPE]
    if selected_type in valid_types:
        if selected_type == 'TRANSFER':
            txns = txns.filter(transaction_type__in=['TRANSFER', 'TRANSFER_DEBIT'])
        else:
            txns = txns.filter(transaction_type=selected_type)
    else:
        selected_type = ''

    if selected_flow == 'credit':
        txns = txns.filter(transaction_type__in=['DEPOSIT', 'LOAN_CREDIT', 'TRANSFER_CREDIT', 'FD_MATURITY_CREDIT', 'FD_CLOSURE_CREDIT'])
    elif selected_flow == 'debit':
        txns = txns.filter(transaction_type__in=['WITHDRAW', 'TRANSFER', 'TRANSFER_DEBIT', 'FD_DEBIT', 'LOAN_REPAYMENT_DEBIT'])
    else:
        selected_flow = ''

    if date_from_raw:
        try:
            d_from = datetime.strptime(date_from_raw, '%Y-%m-%d').date()
            txns = txns.filter(created_at__date__gte=d_from)
        except (ValueError, TypeError):
            date_from_raw = ''

    if date_to_raw:
        try:
            d_to = datetime.strptime(date_to_raw, '%Y-%m-%d').date()
            txns = txns.filter(created_at__date__lte=d_to)
        except (ValueError, TypeError):
            date_to_raw = ''

    # Build querystring preserving existing GET parameters except 'page'
    query_dict = request.GET.copy()
    if 'page' in query_dict:
        query_dict.pop('page')
    filter_querystring = query_dict.urlencode()

    # Pagination: 20 per page
    paginator = Paginator(txns, 20)
    page_param = request.GET.get('page')
    try:
        page_num = int(page_param)
        if page_num < 1:
            page_obj = paginator.page(1)
        elif page_num > paginator.num_pages:
            page_obj = paginator.page(paginator.num_pages if paginator.num_pages > 0 else 1)
        else:
            page_obj = paginator.page(page_num)
    except (TypeError, ValueError, PageNotAnInteger):
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages if paginator.num_pages > 0 else 1)

    return render(request, 'history.html', {
        'account': account,
        'page_obj': page_obj,
        'transactions': page_obj.object_list,
        'filter_querystring': filter_querystring,
        'selected_type': selected_type,
        'selected_flow': selected_flow,
        'date_from': date_from_raw,
        'date_to': date_to_raw,
        'type_choices': Transaction.TRANSACTION_TYPE,
    })


# =========================
# ⭐ PRO PDF STATEMENT
# =========================
@role_required('CUSTOMER')
def passbook_pdf_view(request, account_id=None):

    account_id = account_id or request.GET.get('account_id') or request.POST.get('account_id')
    if account_id:
        account = get_object_or_404(BankAccount, id=account_id, user=request.user)
    else:
        account = BankAccount.objects.filter(user=request.user).last()
        if not account:
            raise Http404("No account found for user.")

    txns = Transaction.objects.filter(account=account, account__user=request.user).order_by('-created_at')

    # Apply identical filters:
    selected_type = (request.GET.get('type') or request.GET.get('transaction_type') or '').strip().upper()
    selected_flow = (request.GET.get('flow') or request.GET.get('direction') or '').strip().lower()
    date_from_raw = (request.GET.get('date_from') or request.GET.get('from') or '').strip()
    date_to_raw = (request.GET.get('date_to') or request.GET.get('to') or '').strip()

    valid_types = [t[0] for t in Transaction.TRANSACTION_TYPE]
    if selected_type in valid_types:
        if selected_type == 'TRANSFER':
            txns = txns.filter(transaction_type__in=['TRANSFER', 'TRANSFER_DEBIT'])
        else:
            txns = txns.filter(transaction_type=selected_type)

    if selected_flow == 'credit':
        txns = txns.filter(transaction_type__in=['DEPOSIT', 'LOAN_CREDIT', 'TRANSFER_CREDIT', 'FD_MATURITY_CREDIT', 'FD_CLOSURE_CREDIT'])
    elif selected_flow == 'debit':
        txns = txns.filter(transaction_type__in=['WITHDRAW', 'TRANSFER', 'TRANSFER_DEBIT', 'FD_DEBIT', 'LOAN_REPAYMENT_DEBIT'])

    if date_from_raw:
        try:
            d_from = datetime.strptime(date_from_raw, '%Y-%m-%d').date()
            txns = txns.filter(created_at__date__gte=d_from)
        except (ValueError, TypeError):
            pass

    if date_to_raw:
        try:
            d_to = datetime.strptime(date_to_raw, '%Y-%m-%d').date()
            txns = txns.filter(created_at__date__lte=d_to)
        except (ValueError, TypeError):
            pass

    # Safe bounded slice to prevent excessive memory consumption
    total_matching = txns.count()
    transactions = list(txns[:MAX_PDF_RECORDS])
    is_truncated = total_matching > MAX_PDF_RECORDS

    response = HttpResponse(content_type='application/pdf')
    response['Content-Disposition'] = 'attachment; filename="bank_statement.pdf"'

    doc = SimpleDocTemplate(response, pagesize=A4, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
    styles = getSampleStyleSheet()

    elements = []

    # 🏦 BANK HEADER
    elements.append(Paragraph("<b>BEBANK — Official Account Statement</b>", styles['Title']))
    elements.append(Spacer(1, 10))

    # 👤 CUSTOMER DETAILS
    elements.append(Paragraph(f"<b>Account Holder:</b> {request.user.username}", styles['Normal']))
    elements.append(Paragraph(f"<b>Account Number:</b> {account.account_number}", styles['Normal']))
    elements.append(Paragraph(f"<b>Account Type:</b> {account.account_type}", styles['Normal']))
    elements.append(Spacer(1, 10))

    # TABLE: Date, Type/Description, Credit, Debit, Balance
    data = [["Date & Time", "Type / Description", "Credit (₹)", "Debit (₹)", "Balance (₹)"]]

    for t in transactions:
        credit = f"{t.amount:.2f}" if t.is_credit() else ""
        debit = f"{t.amount:.2f}" if not t.is_credit() else ""
        balance = f"{t.balance_after:.2f}" if t.balance_after is not None else "N/A"

        type_desc = t.get_transaction_type_display()
        if t.related_account:
            if t.transaction_type == 'TRANSFER_DEBIT':
                type_desc = f"{type_desc} (To {t.related_account.account_number})"
            elif t.transaction_type == 'TRANSFER_CREDIT':
                type_desc = f"{type_desc} (From {t.related_account.account_number})"

        data.append([
            t.created_at.strftime("%d-%m-%Y %H:%M"),
            type_desc,
            credit,
            debit,
            balance
        ])

    table = Table(data, colWidths=[90, 175, 75, 75, 85])

    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0a192f')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#cbd5e1')),
        ('ALIGN', (2, 1), (-1, -1), 'RIGHT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))

    elements.append(table)
    elements.append(Spacer(1, 15))

    if is_truncated:
        elements.append(Paragraph(
            f"<i>Note: Displaying the most recent {MAX_PDF_RECORDS} of {total_matching} records. Apply date filters for specific intervals.</i>",
            styles['Italic']
        ))
        elements.append(Spacer(1, 10))

    elements.append(Paragraph(f"<b>Current Available Balance: ₹{account.balance}</b>", styles['Heading2']))

    doc.build(elements)
    return response


# =========================
# FIXED DEPOSIT WORKFLOW
# =========================
def get_simulation_interest_rate(months):
    """
    Illustrative simulation annual interest rates for academic demo purposes.
    Rate schedule (p.a.):
      3-5 months: 5.00%
      6-11 months: 6.00%
      12-23 months: 7.00%
      24-35 months: 7.25%
      36-60 months: 7.50%
    """
    try:
        m = int(months)
    except (ValueError, TypeError):
        m = 12
    if m < 6:
        return Decimal('5.00')
    elif m < 12:
        return Decimal('6.00')
    elif m < 24:
        return Decimal('7.00')
    elif m < 36:
        return Decimal('7.25')
    else:
        return Decimal('7.50')


@role_required('CUSTOMER')
def fd_view(request, fd_id=None):

    fd_target_id = fd_id or request.GET.get('fd_id') or request.POST.get('fd_id')
    if fd_target_id:
        target_fd = get_object_or_404(FixedDeposit, id=fd_target_id, account__user=request.user)
        if request.method == 'GET':
            return render(request, 'fd_detail.html', {'fd': target_fd})

    account_id = request.POST.get("account_id") or request.GET.get("account_id")
    if account_id:
        account = get_object_or_404(BankAccount, id=account_id, user=request.user)
    else:
        account = BankAccount.objects.filter(user=request.user).last()

    if request.method == 'POST':

        raw_amount = request.POST.get("amount")
        raw_months = request.POST.get("months")
        raw_key = request.POST.get("idempotency_key", "").strip()

        if raw_key:
            try:
                key_uuid = uuid.UUID(raw_key)
                reference_id = f"FD-OPEN-{key_uuid.hex.upper()}"
            except (ValueError, AttributeError):
                messages.error(request, "Malformed transaction idempotency key ❌")
                return redirect("fd")
        else:
            reference_id = f"FD-OPEN-{uuid.uuid4().hex.upper()}"

        try:
            amount = Decimal(raw_amount)
        except (TypeError, ValueError, InvalidOperation):
            messages.error(request, "Invalid amount ❌")
            return redirect("fd")

        if amount <= 0:
            messages.error(request, "Amount must be greater than 0 ❌")
            return redirect("fd")

        if amount < Decimal('100.00'):
            messages.error(request, "Minimum Fixed Deposit amount is ₹100.00 ❌")
            return redirect("fd")

        try:
            months = int(raw_months)
        except (TypeError, ValueError):
            messages.error(request, "Invalid duration in months ❌")
            return redirect("fd")

        if months < 3 or months > 60:
            messages.error(request, "FD duration must be between 3 and 60 months ❌")
            return redirect("fd")

        if not account:
            messages.error(request, "Account not found ❌")
            return redirect("fd")

        if account.user.account_locked or not account.user.is_active:
            messages.error(request, "Your account is locked or inactive ❌")
            return redirect("fd")

        if account.balance < amount:
            messages.error(request, "Insufficient balance ❌")
            return redirect("fd")

        # Idempotency check before write
        existing_tx = Transaction.objects.filter(
            reference_id=reference_id,
            transaction_type='FD_DEBIT'
        ).first()
        if existing_tx:
            messages.info(request, "Fixed Deposit already booked successfully ✅")
            return redirect("fd")

        rate = get_simulation_interest_rate(months)

        try:
            with transaction.atomic():
                locked_account = get_object_or_404(
                    BankAccount.objects.select_for_update(),
                    id=account.id,
                    user=request.user
                )

                if locked_account.user.account_locked or not locked_account.user.is_active:
                    messages.error(request, "Your account is locked or inactive ❌")
                    return redirect("fd")

                if locked_account.is_frozen:
                    messages.error(request, "Cannot open a fixed deposit from a frozen account ❌")
                    return redirect("fd")

                if locked_account.balance < amount:
                    messages.error(request, "Insufficient balance ❌")
                    return redirect("fd")

                balance_before = locked_account.balance
                balance_after = balance_before - amount

                locked_account.balance = balance_after
                locked_account.save(update_fields=['balance'])

                fd = FixedDeposit.objects.create(
                    account=locked_account,
                    amount=amount,
                    interest_rate=rate,
                    duration_months=months,
                    status='ACTIVE'
                )

                Transaction.objects.create(
                    account=locked_account,
                    transaction_type='FD_DEBIT',
                    amount=amount,
                    balance_before=balance_before,
                    balance_after=balance_after,
                    reference_id=reference_id
                )

        except IntegrityError:
            messages.info(request, "Fixed Deposit already booked successfully ✅")
            return redirect("fd")

        send_in_app_notification(
            recipient=request.user,
            notification_type='FD',
            title="Fixed Deposit Created",
            message=f"Fixed Deposit #{fd.id} booked for ₹{amount:,.2f} ({months} months @ {rate}% p.a.).",
            target_url="/bank/fd/",
            dedup_key=f"FD-BOOK-NOTIF-{reference_id}"
        )

        messages.success(request, f"FD Booked Successfully 🎉 (₹{amount} for {months} months @ {rate}% p.a.)")
        return redirect("fd")

    # GET: Query customer FDs, summary metrics, and status filter
    user_accounts = BankAccount.objects.filter(user=request.user).order_by('-created_at')
    fds = FixedDeposit.objects.filter(account__user=request.user).select_related('account').order_by('-created_at')

    status_filter = request.GET.get('status', 'all').upper()
    if status_filter in ('ACTIVE', 'MATURED', 'CLOSED'):
        fds = fds.filter(status=status_filter)

    search_query = request.GET.get('q', '').strip()
    if search_query:
        fds = fds.filter(
            Q(amount__icontains=search_query) |
            Q(account__account_number__icontains=search_query) |
            Q(id__icontains=search_query)
        )

    all_user_fds = FixedDeposit.objects.filter(account__user=request.user)
    total_active_principal = all_user_fds.filter(status='ACTIVE').aggregate(Sum('amount'))['amount__sum'] or Decimal('0.00')
    total_active_maturity = all_user_fds.filter(status='ACTIVE').aggregate(Sum('maturity_amount'))['maturity_amount__sum'] or Decimal('0.00')
    active_count = all_user_fds.filter(status='ACTIVE').count()

    idempotency_key = str(uuid.uuid4())

    return render(request, 'fd.html', {
        'accounts': user_accounts,
        'account': account,
        'fds': fds,
        'total_active_principal': total_active_principal,
        'total_active_maturity': total_active_maturity,
        'active_count': active_count,
        'idempotency_key': idempotency_key,
        'status_filter': status_filter,
        'search_query': search_query,
    })


@role_required('CUSTOMER')
def fd_detail_view(request, fd_id):
    fd = get_object_or_404(FixedDeposit, id=fd_id, account__user=request.user)
    return render(request, 'fd_detail.html', {'fd': fd})


@require_POST
@role_required('CUSTOMER')
def fd_settle_view(request, fd_id):
    fd = get_object_or_404(FixedDeposit, id=fd_id, account__user=request.user)

    if fd.status != 'ACTIVE':
        messages.error(request, "This Fixed Deposit is already settled or closed ❌")
        return redirect("fd")

    if timezone.now() < fd.maturity_date:
        messages.error(
            request,
            f"This Fixed Deposit has not reached its maturity date yet ({fd.maturity_date.strftime('%d-%m-%Y')}) ❌"
        )
        return redirect("fd")

    reference_id = f"FD-MAT-{fd.id}"

    # Idempotency check: verify whether already processed
    existing_tx = Transaction.objects.filter(
        reference_id=reference_id,
        transaction_type='FD_MATURITY_CREDIT'
    ).first()
    if existing_tx:
        if fd.status != 'MATURED':
            fd.status = 'MATURED'
            fd.save(update_fields=['status'])
        messages.info(request, "This Fixed Deposit has already been settled ✅")
        return redirect("fd")

    try:
        with transaction.atomic():
            locked_account = BankAccount.objects.select_for_update().get(id=fd.account_id)

            if locked_account.user.account_locked or not locked_account.user.is_active:
                messages.error(request, "Account holder is locked or inactive ❌")
                return redirect("fd")

            locked_fd = FixedDeposit.objects.select_for_update().get(id=fd.id)
            if locked_fd.status != 'ACTIVE' or timezone.now() < locked_fd.maturity_date:
                messages.error(request, "Fixed Deposit is not eligible for maturity settlement ❌")
                return redirect("fd")

            payout = locked_fd.maturity_amount
            balance_before = locked_account.balance
            balance_after = balance_before + payout

            locked_account.balance = balance_after
            locked_account.save(update_fields=['balance'])

            locked_fd.status = 'MATURED'
            locked_fd.settled_at = timezone.now()
            locked_fd.settlement_amount = payout
            locked_fd.closure_type = 'MATURITY'
            locked_fd.save(update_fields=['status', 'settled_at', 'settlement_amount', 'closure_type'])

            Transaction.objects.create(
                account=locked_account,
                transaction_type='FD_MATURITY_CREDIT',
                amount=payout,
                balance_before=balance_before,
                balance_after=balance_after,
                reference_id=reference_id
            )

    except IntegrityError:
        messages.info(request, "This Fixed Deposit has already been settled ✅")
        return redirect("fd")

    send_in_app_notification(
        recipient=fd.account.user,
        notification_type='FD',
        title="Fixed Deposit Matured",
        message=f"Fixed Deposit #{fd.id} matured. ₹{payout:,.2f} credited to your account {locked_account.account_number}.",
        target_url="/bank/fd/",
        dedup_key=f"FD-SETTLE-NOTIF-{reference_id}"
    )

    messages.success(
        request,
        f"Fixed Deposit #{fd.id} settled successfully! ₹{payout} credited to account {locked_account.account_number} ✅"
    )
    return redirect("fd")


@require_POST
@role_required('CUSTOMER')
def fd_close_view(request, fd_id):
    fd = get_object_or_404(FixedDeposit, id=fd_id, account__user=request.user)

    if fd.status != 'ACTIVE':
        messages.error(request, "Only active Fixed Deposits can be prematurely closed ❌")
        return redirect("fd")

    reference_id = f"FD-CLOSE-{fd.id}"

    # Idempotency check: verify whether already processed
    existing_tx = Transaction.objects.filter(
        reference_id=reference_id,
        transaction_type='FD_CLOSURE_CREDIT'
    ).first()
    if existing_tx:
        if fd.status != 'CLOSED':
            fd.status = 'CLOSED'
            fd.save(update_fields=['status'])
        messages.info(request, "This Fixed Deposit has already been closed ✅")
        return redirect("fd")

    closure_reason = request.POST.get('closure_reason', '').strip()
    if not closure_reason:
        closure_reason = "Premature closure requested by customer. Principal returned in full; accrued interest forfeited."

    try:
        with transaction.atomic():
            locked_account = BankAccount.objects.select_for_update().get(id=fd.account_id)

            if locked_account.user.account_locked or not locked_account.user.is_active:
                messages.error(request, "Account holder is locked or inactive ❌")
                return redirect("fd")

            locked_fd = FixedDeposit.objects.select_for_update().get(id=fd.id)
            if locked_fd.status != 'ACTIVE':
                messages.error(request, "Fixed Deposit is no longer active ❌")
                return redirect("fd")

            payout = locked_fd.amount
            balance_before = locked_account.balance
            balance_after = balance_before + payout

            locked_account.balance = balance_after
            locked_account.save(update_fields=['balance'])

            locked_fd.status = 'CLOSED'
            locked_fd.settled_at = timezone.now()
            locked_fd.settlement_amount = payout
            locked_fd.closure_type = 'PREMATURE'
            locked_fd.closure_reason = closure_reason
            locked_fd.save(update_fields=['status', 'settled_at', 'settlement_amount', 'closure_type', 'closure_reason'])

            Transaction.objects.create(
                account=locked_account,
                transaction_type='FD_CLOSURE_CREDIT',
                amount=payout,
                balance_before=balance_before,
                balance_after=balance_after,
                reference_id=reference_id
            )

    except IntegrityError:
        messages.info(request, "This Fixed Deposit has already been closed ✅")
        return redirect("fd")

    send_in_app_notification(
        recipient=fd.account.user,
        notification_type='FD',
        title="Fixed Deposit Closed Prematurely",
        message=f"Fixed Deposit #{fd.id} closed prematurely. Principal ₹{payout:,.2f} credited to your account {locked_account.account_number}.",
        target_url="/bank/fd/",
        dedup_key=f"FD-CLOSE-NOTIF-{reference_id}"
    )

    messages.success(
        request,
        f"Fixed Deposit #{fd.id} closed prematurely. Principal of ₹{payout} credited to account {locked_account.account_number}. Accrued interest was forfeited ✅"
    )
    return redirect("fd")


# =========================
# LOAN SIMULATION & CALCULATION HELPERS
# =========================
def get_simulation_loan_interest_rate(months):
    """
    Illustrative simulation annual interest rates for customer loan products.
    Academic simulation schedule:
      6 - 12 months: 9.50%
      13 - 36 months: 10.50%
      37 - 60 months: 11.50%
      61 - 120 months: 12.00%
      Other / default: 10.50%
    """
    try:
        m = int(months)
    except (TypeError, ValueError):
        return Decimal('10.50')

    if 6 <= m <= 12:
        return Decimal('9.50')
    elif 13 <= m <= 36:
        return Decimal('10.50')
    elif 37 <= m <= 60:
        return Decimal('11.50')
    elif 61 <= m <= 120:
        return Decimal('12.00')
    return Decimal('10.50')


def calculate_reducing_emi(principal, annual_rate, months):
    """
    Standard reducing-balance monthly EMI formula:
      EMI = [P * r * (1 + r)^n] / [(1 + r)^n - 1]
    where r = annual_rate / 1200, n = months.
    Zero-interest case: EMI = P / n.
    """
    P = Decimal(str(principal))
    R = Decimal(str(annual_rate))
    n = int(months)
    if n <= 0:
        return Decimal('0.00')
    if R <= Decimal('0.00'):
        return (P / n).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    r = R / Decimal('1200')
    factor = (Decimal('1') + r) ** n
    emi = (P * r * factor) / (factor - Decimal('1'))
    return emi.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def generate_loan_schedule(loan):
    """
    Generates deterministic amortization schedule for an approved loan.
    Guarantees:
      - Reducing-balance monthly interest calculation
      - Last installment adjustment: absorbs cumulative rounding difference so
        total principal paid equals exactly principal P and closing balance reaches ₹0.00
      - Persists LoanInstallment objects
      - Updates loan with emi_amount, total_interest, total_payable, outstanding_balance
    """
    P = Decimal(str(loan.amount))
    R = Decimal(str(loan.interest_rate))
    n = int(loan.duration_months)
    emi = calculate_reducing_emi(P, R, n)
    r = R / Decimal('1200') if R > Decimal('0.00') else Decimal('0.00')

    base_date = loan.disbursed_at.date() if loan.disbursed_at else timezone.now().date()
    balance = P
    total_interest = Decimal('0.00')
    total_payable = Decimal('0.00')
    installments_to_create = []

    for k in range(1, n + 1):
        interest = (balance * r).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        if k == n:
            principal_comp = balance
            due = (principal_comp + interest).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            closing = Decimal('0.00')
        else:
            principal_comp = emi - interest
            closing = balance - principal_comp
            due = emi

        due_date = add_months(base_date, k)
        if hasattr(due_date, 'date'):
            due_date = due_date.date()

        total_interest += interest
        total_payable += due

        installments_to_create.append(LoanInstallment(
            loan=loan,
            installment_number=k,
            due_date=due_date,
            opening_principal=balance,
            principal_component=principal_comp,
            interest_component=interest,
            emi_due=due,
            amount_paid=Decimal('0.00'),
            status='PENDING'
        ))
        balance = closing

    LoanInstallment.objects.filter(loan=loan).delete()
    LoanInstallment.objects.bulk_create(installments_to_create)

    loan.emi_amount = emi
    loan.total_interest = total_interest
    loan.total_payable = total_payable
    loan.outstanding_balance = total_payable
    loan.save(update_fields=['emi_amount', 'total_interest', 'total_payable', 'outstanding_balance'])


# =========================
# LOAN WORKSPACE & DETAIL
# =========================
@role_required('CUSTOMER')
def loan_view(request, loan_id=None):
    if loan_id:
        return loan_detail_view(request, loan_id)

    # Validate target loan ID if passed in query param
    loan_target_id = request.GET.get('loan_id')
    if loan_target_id:
        return loan_detail_view(request, loan_target_id)

    accounts = BankAccount.objects.filter(user=request.user).order_by('id')
    if not accounts.exists():
        messages.error(request, "You must have an active bank account to view or apply for loans ❌")
        return redirect("customer_dashboard")

    if request.method == 'POST':
        account_id = request.POST.get("account_id")
        amount = request.POST.get("amount")
        months = request.POST.get("months")

        if account_id:
            account = get_object_or_404(BankAccount, id=account_id, user=request.user)
        else:
            account = accounts.first()

        if not account:
            messages.error(request, "Designated bank account not found or access denied ❌")
            return redirect("loan")

        if request.user.account_locked or not request.user.is_active:
            messages.error(request, "Account holder is locked or inactive ❌")
            return redirect("loan")

        try:
            amount = Decimal(str(amount)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        except (TypeError, ValueError, InvalidOperation):
            messages.error(request, "Invalid loan amount ❌")
            return redirect("loan")

        if amount <= Decimal('0.00'):
            messages.error(request, "Amount must be greater than 0 ❌")
            return redirect("loan")

        if amount > Decimal('1000000.00'):
            messages.error(request, "Maximum loan amount for online simulation is ₹10,00,000.00 ❌")
            return redirect("loan")

        try:
            months = int(months)
        except (TypeError, ValueError):
            messages.error(request, "Invalid duration in months ❌")
            return redirect("loan")

        if months < 6 or months > 120:
            messages.error(request, "Loan tenure must be between 6 and 120 months ❌")
            return redirect("loan")

        rate = get_simulation_loan_interest_rate(months)
        estimated_emi = calculate_reducing_emi(amount, rate, months)

        loan_obj = Loan.objects.create(
            account=account,
            amount=amount,
            duration_months=months,
            interest_rate=rate,
            emi_amount=estimated_emi,
            status="PENDING"
        )

        send_in_app_notification(
            recipient=request.user,
            notification_type='LOAN',
            title="Loan Application Submitted",
            message=f"Your loan application for ₹{amount:,.2f} ({months} months) has been received and is under review.",
            target_url=f"/bank/loan/{loan_obj.id}/"
        )

        messages.success(request, f"Loan application for ₹{amount} submitted successfully! Your application is under review 🎉")
        return redirect("loan")

    # Metrics calculation
    customer_loans = Loan.objects.filter(account__user=request.user).select_related('account').order_by('-created_at')

    total_borrowed = customer_loans.filter(status__in=['APPROVED', 'CLOSED']).aggregate(Sum('amount'))['amount__sum'] or Decimal('0.00')
    total_outstanding = customer_loans.filter(status='APPROVED').aggregate(Sum('outstanding_balance'))['outstanding_balance__sum'] or Decimal('0.00')
    active_loans_count = customer_loans.filter(status='APPROVED').count()
    pending_loans_count = customer_loans.filter(status__in=['PENDING', 'UNDER_REVIEW']).count()

    simulation_rates = [
        {'tenure': '6 – 12 months', 'rate': '9.50% p.a.'},
        {'tenure': '13 – 36 months', 'rate': '10.50% p.a.'},
        {'tenure': '37 – 60 months', 'rate': '11.50% p.a.'},
        {'tenure': '61 – 120 months', 'rate': '12.00% p.a.'},
    ]

    return render(request, 'loan.html', {
        'accounts': accounts,
        'loans': customer_loans,
        'total_borrowed': total_borrowed,
        'total_outstanding': total_outstanding,
        'active_loans_count': active_loans_count,
        'pending_loans_count': pending_loans_count,
        'simulation_rates': simulation_rates,
    })


@role_required('CUSTOMER')
def loan_detail_view(request, loan_id):
    loan = get_object_or_404(
        Loan.objects.select_related('account', 'account__user'),
        id=loan_id,
        account__user=request.user
    )
    installments = loan.installments.all().order_by('installment_number')
    repayments = loan.repayments.all().select_related('account').order_by('-payment_date')
    accounts = BankAccount.objects.filter(user=request.user).order_by('id')

    # Next pending installment
    next_installment = installments.filter(status__in=['PENDING', 'PARTIALLY_PAID', 'OVERDUE']).first()

    idempotency_key = str(uuid.uuid4())

    return render(request, 'loan_detail.html', {
        'loan': loan,
        'installments': installments,
        'repayments': repayments,
        'accounts': accounts,
        'next_installment': next_installment,
        'idempotency_key': idempotency_key,
    })


# =========================
# APPROVE LOAN
# =========================
# APPROVE LOAN (ADMIN ONLY — ATOMIC DISBURSEMENT & AUDIT)
# =========================
@role_required('ADMIN')
@require_POST
@transaction.atomic
def approve_loan(request, loan_id):

    # Lock loan row to prevent concurrent duplicate processing
    loan = get_object_or_404(Loan.objects.select_for_update(), id=loan_id)

    if loan.status not in ["PENDING", "UNDER_REVIEW"]:
        messages.error(request, f"Loan #{loan_id} has already been processed ({loan.status}). ❌")
        return redirect(request.POST.get('next') or '/admin-dashboard/')

    # Lock account row to guarantee balance integrity
    account = get_object_or_404(BankAccount.objects.select_for_update(), id=loan.account_id)

    reason = request.POST.get('reason', '').strip()

    balance_before = account.balance
    balance_after = balance_before + loan.amount
    account.balance = balance_after
    account.save(update_fields=['balance'])

    reference_id = f"LOAN-DISB-{loan.id}"
    Transaction.objects.create(
        account=account,
        transaction_type="LOAN_CREDIT",
        amount=loan.amount,
        balance_before=balance_before,
        balance_after=balance_after,
        reference_id=reference_id
    )

    loan.status = "APPROVED"
    loan.disbursed_at = timezone.now()
    if reason:
        loan.admin_decision_reason = reason
    loan.admin_decided_by = request.user
    if not loan.interest_rate:
        loan.interest_rate = get_simulation_loan_interest_rate(loan.duration_months)
    loan.save(update_fields=['status', 'disbursed_at', 'interest_rate', 'admin_decision_reason', 'admin_decided_by'])

    # Record in LoanVerificationLog
    LoanVerificationLog.objects.create(
        loan=loan,
        performed_by=request.user,
        action='ADMIN_APPROVE',
        notes=reason or f"Approved and disbursed by administrator {request.user.username}."
    )
    log_employee_activity(
        employee=request.user,
        action_type='LOAN_DECISION_APPROVE',
        customer=account.user,
        account_number=account.account_number,
        target_id=str(loan.id),
        description=f"Admin approved loan #{loan.id} (₹{loan.amount}). Reason: {reason or 'Approved by admin'}",
        request=request
    )

    # Generate full amortization schedule exactly once
    generate_loan_schedule(loan)

    send_in_app_notification(
        recipient=account.user,
        notification_type='LOAN',
        title="Loan Approved & Disbursed",
        message=f"Loan #{loan_id} for ₹{loan.amount:,.2f} has been approved and disbursed to your account {account.account_number}.",
        target_url=f"/bank/loan/{loan.id}/",
        dedup_key=f"LOAN-APPROVE-NOTIF-{loan.id}"
    )

    messages.success(request, f"Loan #{loan_id} approved successfully! ₹{loan.amount} disbursed to account {account.account_number} ✅")
    return redirect(request.POST.get('next') or '/admin-dashboard/')


# =========================
# REJECT LOAN (ADMIN ONLY — RECORDED REASON & AUDIT)
# =========================
@role_required('ADMIN')
@require_POST
@transaction.atomic
def reject_loan(request, loan_id):

    loan = get_object_or_404(Loan.objects.select_for_update(), id=loan_id)

    if loan.status not in ["PENDING", "UNDER_REVIEW"]:
        messages.error(request, f"Loan #{loan_id} has already been processed ({loan.status}). ❌")
        return redirect(request.POST.get('next') or '/admin-dashboard/')

    reason = request.POST.get('reason', '').strip() or "Administrative decision."

    loan.status = "REJECTED"
    loan.admin_decision_reason = reason
    loan.admin_decided_by = request.user
    loan.save(update_fields=['status', 'admin_decision_reason', 'admin_decided_by'])

    # Record in LoanVerificationLog
    LoanVerificationLog.objects.create(
        loan=loan,
        performed_by=request.user,
        action='ADMIN_REJECT',
        notes=reason
    )
    log_employee_activity(
        employee=request.user,
        action_type='LOAN_DECISION_REJECT',
        customer=loan.account.user if loan.account else None,
        account_number=loan.account.account_number if loan.account else '',
        target_id=str(loan.id),
        description=f"Admin rejected loan #{loan.id}. Reason: {reason}",
        request=request
    )

    if loan.account and loan.account.user:
        send_in_app_notification(
            recipient=loan.account.user,
            notification_type='LOAN',
            title="Loan Application Rejected",
            message=f"Your loan application #{loan.id} (₹{loan.amount:,.2f}) was rejected by bank administration. Reason: {reason}",
            target_url="/bank/loan/",
            dedup_key=f"LOAN-REJECT-NOTIF-{loan.id}"
        )

    messages.success(request, f"Loan #{loan_id} rejected. 🚫")
    return redirect(request.POST.get('next') or '/admin-dashboard/')


# =========================
# LOAN REPAYMENT WORKFLOW
# =========================
@role_required('CUSTOMER')
@require_POST
def loan_repay_view(request, loan_id):
    loan = get_object_or_404(Loan, id=loan_id, account__user=request.user)

    if loan.status != "APPROVED":
        messages.error(request, f"Loan #{loan_id} is not active for repayments (status: {loan.status}). ❌")
        return redirect("loan_detail", loan_id=loan.id)

    if not loan.outstanding_balance or loan.outstanding_balance <= Decimal('0.00'):
        messages.info(request, f"Loan #{loan_id} has already been fully paid! 🎉")
        return redirect("loan_detail", loan_id=loan.id)

    # ⭐ Backend Idempotency Validation
    raw_key = request.POST.get('idempotency_key', '').strip()
    if not raw_key:
        messages.error(request, "Missing transaction idempotency key ❌")
        return redirect("loan_detail", loan_id=loan.id)

    try:
        key_uuid = uuid.UUID(raw_key)
    except (ValueError, AttributeError):
        messages.error(request, "Malformed transaction idempotency key ❌")
        return redirect("loan_detail", loan_id=loan.id)

    repay_ref = f"LOAN-REPAY-{key_uuid.hex.upper()}"

    raw_amount = request.POST.get('amount')
    try:
        repay_amount = Decimal(str(raw_amount)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    except (TypeError, ValueError, InvalidOperation):
        messages.error(request, "Invalid repayment amount ❌")
        return redirect("loan_detail", loan_id=loan.id)

    if repay_amount <= Decimal('0.00'):
        messages.error(request, "Repayment amount must be greater than zero ❌")
        return redirect("loan_detail", loan_id=loan.id)

    if repay_amount > loan.outstanding_balance:
        messages.error(
            request,
            f"Repayment amount (₹{repay_amount}) cannot exceed remaining outstanding balance of ₹{loan.outstanding_balance} ❌"
        )
        return redirect("loan_detail", loan_id=loan.id)

    account_id = request.POST.get('account_id')
    if account_id:
        target_account = get_object_or_404(BankAccount, id=account_id, user=request.user)
    else:
        target_account = loan.account

    # Idempotency check before write
    existing_repay = LoanRepayment.objects.filter(reference_id=repay_ref).first()
    if existing_repay:
        if (existing_repay.loan_id == loan.id and
            existing_repay.account_id == target_account.id and
            existing_repay.amount == repay_amount):
            messages.info(request, "Repayment already processed successfully ✅")
            return redirect("loan_detail", loan_id=loan.id)
        else:
            messages.error(request, "Idempotency key has already been used for a different repayment ❌")
            return redirect("loan_detail", loan_id=loan.id)

    try:
        with transaction.atomic():
            locked_account = BankAccount.objects.select_for_update().get(id=target_account.id)
            locked_loan = Loan.objects.select_for_update().get(id=loan.id)

            if locked_account.user.account_locked or not locked_account.user.is_active:
                messages.error(request, "Account holder is locked or inactive ❌")
                return redirect("loan_detail", loan_id=loan.id)

            if locked_account.is_frozen:
                messages.error(request, "Account is frozen. Loan repayments from this account are prohibited ❌")
                return redirect("loan_detail", loan_id=loan.id)

            if locked_loan.status != 'APPROVED':
                messages.error(request, "Loan is no longer active for repayment ❌")
                return redirect("loan_detail", loan_id=loan.id)

            # Concurrent idempotency re-check under lock
            existing_under_lock = LoanRepayment.objects.select_for_update().filter(reference_id=repay_ref).first()
            if existing_under_lock:
                if (existing_under_lock.loan_id == locked_loan.id and
                    existing_under_lock.account_id == locked_account.id and
                    existing_under_lock.amount == repay_amount):
                    messages.info(request, "Repayment already processed successfully ✅")
                    return redirect("loan_detail", loan_id=loan.id)
                else:
                    messages.error(request, "Idempotency key has already been used for a different repayment ❌")
                    return redirect("loan_detail", loan_id=loan.id)

            if locked_loan.outstanding_balance < repay_amount:
                messages.error(
                    request,
                    f"Repayment amount exceeds remaining loan balance of ₹{locked_loan.outstanding_balance} ❌"
                )
                return redirect("loan_detail", loan_id=loan.id)

            if locked_account.balance < repay_amount:
                messages.error(
                    request,
                    f"Insufficient funds in account {locked_account.account_number}. Available: ₹{locked_account.balance}, Required: ₹{repay_amount} ❌"
                )
                return redirect("loan_detail", loan_id=loan.id)

            # Deduct balance
            bal_before = locked_account.balance
            bal_after = bal_before - repay_amount
            locked_account.balance = bal_after
            locked_account.save(update_fields=['balance'])

            # Record Ledger Transaction
            Transaction.objects.create(
                account=locked_account,
                transaction_type='LOAN_REPAYMENT_DEBIT',
                amount=repay_amount,
                balance_before=bal_before,
                balance_after=bal_after,
                reference_id=repay_ref
            )

            # Create LoanRepayment record
            LoanRepayment.objects.create(
                loan=locked_loan,
                account=locked_account,
                amount=repay_amount,
                reference_id=repay_ref
            )

            # Allocate to installments (oldest due first)
            remaining_to_allocate = repay_amount
            pending_installments = locked_loan.installments.filter(
                status__in=['PENDING', 'PARTIALLY_PAID', 'OVERDUE']
            ).order_by('installment_number')

            for inst in pending_installments:
                if remaining_to_allocate <= Decimal('0.00'):
                    break
                inst_due = inst.emi_due - inst.amount_paid
                if remaining_to_allocate >= inst_due:
                    inst.amount_paid = inst.emi_due
                    inst.status = 'PAID'
                    inst.paid_at = timezone.now()
                    inst.save(update_fields=['amount_paid', 'status', 'paid_at'])
                    remaining_to_allocate -= inst_due
                else:
                    inst.amount_paid += remaining_to_allocate
                    inst.status = 'PARTIALLY_PAID'
                    inst.save(update_fields=['amount_paid', 'status'])
                    remaining_to_allocate = Decimal('0.00')

            # Update loan outstanding balance and status
            locked_loan.outstanding_balance = (locked_loan.outstanding_balance - repay_amount).quantize(
                Decimal('0.01'), rounding=ROUND_HALF_UP
            )
            if locked_loan.outstanding_balance <= Decimal('0.00'):
                locked_loan.outstanding_balance = Decimal('0.00')
                locked_loan.status = 'CLOSED'
                locked_loan.closed_at = timezone.now()
                locked_loan.save(update_fields=['outstanding_balance', 'status', 'closed_at'])
                messages.success(request, f"Loan #{locked_loan.id} has been completely repaid and closed! 🎉")
            else:
                locked_loan.save(update_fields=['outstanding_balance'])
                messages.success(
                    request,
                    f"Repayment of ₹{repay_amount} processed successfully! Remaining balance: ₹{locked_loan.outstanding_balance} ✅"
                )

    except IntegrityError:
        messages.error(request, "A database conflict occurred while processing repayment. Please try again. ❌")
        return redirect("loan_detail", loan_id=loan.id)

    send_in_app_notification(
        recipient=request.user,
        notification_type='LOAN',
        title="Loan Repayment Processed",
        message=f"Repayment of ₹{repay_amount:,.2f} applied to Loan #{loan.id}. Remaining balance: ₹{locked_loan.outstanding_balance:,.2f}.",
        target_url=f"/bank/loan/{loan.id}/",
        dedup_key=f"LOAN-REPAY-NOTIF-{repay_ref}"
    )

    return redirect("loan_detail", loan_id=loan.id)


# =========================
# VIRTUAL DEBIT CARDS WORKFLOW
# =========================
@role_required('CUSTOMER')
def card_list_view(request):
    """Customer virtual debit cards dashboard & issuance workspace."""
    accounts = BankAccount.objects.filter(user=request.user).order_by('id')
    cards = VirtualDebitCard.objects.filter(user=request.user).select_related('account').order_by('-created_at')

    active_card_account_ids = set(cards.filter(status__in=['ACTIVE', 'BLOCKED']).values_list('account_id', flat=True))
    eligible_accounts = [acc for acc in accounts if acc.id not in active_card_account_ids and not getattr(request.user, 'account_locked', False) and not acc.is_frozen]

    active_count = cards.filter(status='ACTIVE').count()
    blocked_count = cards.filter(status='BLOCKED').count()

    return render(request, "card_list.html", {
        'cards': cards,
        'accounts': accounts,
        'eligible_accounts': eligible_accounts,
        'active_count': active_count,
        'blocked_count': blocked_count,
    })


@role_required('CUSTOMER')
@require_POST
def card_issue_view(request):
    """Issues a new simulated virtual debit card for an eligible owned bank account."""
    if request.user.account_locked or not request.user.is_active:
        messages.error(request, "Account holder is locked or inactive ❌")
        return redirect('card_list')

    account_id = request.POST.get('account_id')
    account = get_object_or_404(BankAccount, id=account_id, user=request.user)

    if account.is_frozen:
        messages.error(request, "Cannot issue a debit card for a frozen account ❌")
        return redirect('card_list')

    cardholder_name = request.POST.get('cardholder_name', '').strip()
    if not cardholder_name:
        name = request.user.get_full_name().strip() or request.user.username
        cardholder_name = name.upper()[:100]
    else:
        cardholder_name = cardholder_name.upper()[:100]

    card_type = request.POST.get('card_type', 'CLASSIC').strip().upper()
    valid_types = [choice[0] for choice in VirtualDebitCard.CARD_TYPE_CHOICES]
    if card_type not in valid_types:
        card_type = 'CLASSIC'

    try:
        with transaction.atomic():
            locked_account = BankAccount.objects.select_for_update().get(id=account.id)
            existing_active = VirtualDebitCard.objects.select_for_update().filter(
                account=locked_account,
                status__in=['ACTIVE', 'BLOCKED']
            ).first()

            if existing_active:
                messages.error(
                    request,
                    f"Account {locked_account.account_number} already has an active virtual debit card ({existing_active.masked_card_number}). Only one active card per account is permitted. ❌"
                )
                return redirect('card_list')

            # Generate unique simulation card number
            while True:
                num = generate_simulation_card_number()
                if not VirtualDebitCard.objects.filter(card_number=num).exists():
                    break

            today = timezone.now().date()
            exp_year = today.year + 3
            exp_month = today.month

            daily_limit = Decimal('50000.00') if card_type == 'PLATINUM' else Decimal('25000.00')

            card = VirtualDebitCard.objects.create(
                user=request.user,
                account=locked_account,
                card_number=num,
                cardholder_name=cardholder_name,
                card_type=card_type,
                issue_date=today,
                expiry_month=exp_month,
                expiry_year=exp_year,
                status='ACTIVE',
                daily_limit=daily_limit,
            )

            DebitCardAuditLog.objects.create(
                card=card,
                performed_by=request.user,
                action='ISSUED',
                reason='Customer requested virtual debit card issuance.'
            )

    except IntegrityError:
        messages.error(request, "A database conflict occurred while issuing card. Please try again. ❌")
        return redirect('card_list')

    send_in_app_notification(
        recipient=request.user,
        notification_type='CARD',
        title="Virtual Debit Card Issued",
        message=f"Virtual Debit Card {card.masked_card_number} was successfully issued for account {card.account.account_number}.",
        target_url=f"/bank/cards/{card.id}/",
        dedup_key=f"CARD-ISSUE-{card.id}"
    )

    messages.success(request, f"Virtual Debit Card {card.masked_card_number} issued successfully! 🎉")
    return redirect('card_list')


@role_required('CUSTOMER')
@require_POST
def card_block_view(request, card_id):
    """Customer temporarily blocks their virtual debit card."""
    card = get_object_or_404(VirtualDebitCard, id=card_id, user=request.user)

    if card.status == 'BLOCKED':
        messages.info(request, f"Card {card.masked_card_number} is already blocked.")
        return redirect('card_list')

    if card.status in ['CANCELLED', 'EXPIRED']:
        messages.error(request, f"Cannot block a {card.status.lower()} card ❌")
        return redirect('card_list')

    card.status = 'BLOCKED'
    card.save(update_fields=['status', 'updated_at'])

    DebitCardAuditLog.objects.create(
        card=card,
        performed_by=request.user,
        action='BLOCKED_CUSTOMER',
        reason='Customer requested temporary card block.'
    )

    send_in_app_notification(
        recipient=request.user,
        notification_type='CARD',
        title="Virtual Debit Card Blocked",
        message=f"Your virtual debit card {card.masked_card_number} was temporarily blocked.",
        target_url=f"/bank/cards/{card.id}/"
    )

    messages.success(request, f"Card {card.masked_card_number} has been blocked successfully ✅")
    return redirect('card_list')


@role_required('CUSTOMER')
@require_POST
def card_unblock_view(request, card_id):
    """Customer unblocks their virtual debit card (if not suspended by administration)."""
    card = get_object_or_404(VirtualDebitCard, id=card_id, user=request.user)

    if card.status != 'BLOCKED':
        messages.error(request, f"Only blocked cards can be unblocked (current status: {card.status}) ❌")
        return redirect('card_list')

    if card.blocked_by_admin:
        messages.error(request, "This card was suspended by bank administration and cannot be unblocked online. Please contact support ❌")
        return redirect('card_list')

    if card.is_expired:
        messages.error(request, "This card has expired and cannot be unblocked ❌")
        return redirect('card_list')

    card.status = 'ACTIVE'
    card.save(update_fields=['status', 'updated_at'])

    DebitCardAuditLog.objects.create(
        card=card,
        performed_by=request.user,
        action='UNBLOCKED_CUSTOMER',
        reason='Customer requested card unblock.'
    )

    send_in_app_notification(
        recipient=request.user,
        notification_type='CARD',
        title="Virtual Debit Card Unblocked",
        message=f"Your virtual debit card {card.masked_card_number} was unblocked and is active.",
        target_url=f"/bank/cards/{card.id}/"
    )

    messages.success(request, f"Card {card.masked_card_number} unblocked and active ✅")
    return redirect('card_list')


@role_required('CUSTOMER')
@require_POST
def card_cancel_view(request, card_id):
    """Permanently cancels a virtual debit card."""
    card = get_object_or_404(VirtualDebitCard, id=card_id, user=request.user)

    if card.status == 'CANCELLED':
        messages.info(request, f"Card {card.masked_card_number} is already cancelled.")
        return redirect('card_list')

    card.status = 'CANCELLED'
    card.save(update_fields=['status', 'updated_at'])

    DebitCardAuditLog.objects.create(
        card=card,
        performed_by=request.user,
        action='CANCELLED_CUSTOMER',
        reason='Card permanently cancelled by customer.'
    )

    send_in_app_notification(
        recipient=request.user,
        notification_type='CARD',
        title="Virtual Debit Card Cancelled",
        message=f"Your virtual debit card {card.masked_card_number} has been permanently cancelled.",
        target_url=f"/bank/cards/{card.id}/"
    )

    messages.success(request, f"Card {card.masked_card_number} has been permanently cancelled ✅")
    return redirect('card_list')


@role_required('CUSTOMER')
def card_detail_view(request, card_id):
    """Customer detailed card view with audit trail."""
    card = get_object_or_404(
        VirtualDebitCard.objects.select_related('account', 'user'),
        id=card_id,
        user=request.user
    )
    audit_logs = card.audit_logs.all().select_related('performed_by')[:25]

    return render(request, "card_detail.html", {
        'card': card,
        'audit_logs': audit_logs,
    })


# =========================
# STAFF & ADMIN CARD AUDIT / ACTIONS
# =========================
@role_required('EMPLOYEE', 'ADMIN')
def staff_card_detail_view(request, card_id):
    """Staff inspection of virtual debit card status and complete audit trail."""
    card = get_object_or_404(
        VirtualDebitCard.objects.select_related('account', 'user'),
        id=card_id
    )

    # Record staff inspection audit event
    DebitCardAuditLog.objects.create(
        card=card,
        performed_by=request.user,
        action='STATUS_INSPECTED_STAFF',
        reason=f"Status inspected by {request.user.role} {request.user.username}."
    )
    log_employee_activity(
        employee=request.user,
        action_type='CARD_INSPECTION',
        customer=card.user,
        account_number=card.account.account_number if card.account else '',
        target_id=str(card.id),
        description=f"Inspected Virtual Debit Card #{card.id} (masked {card.masked_card_number})",
        request=request
    )

    audit_logs = card.audit_logs.all().select_related('performed_by')[:50]

    return render(request, "staff_card_detail.html", {
        'card': card,
        'audit_logs': audit_logs,
    })


@role_required('ADMIN')
@require_POST
def admin_card_action_view(request, card_id):
    """Administrative override action (block/unblock/cancel) on a virtual debit card."""
    card = get_object_or_404(VirtualDebitCard, id=card_id)
    action_type = request.POST.get('action_type', '').strip().lower()
    reason = request.POST.get('reason', '').strip() or f"Administrative action by {request.user.username}."
    next_url = request.POST.get('next', '').strip()

    if action_type == 'block':
        card.status = 'BLOCKED'
        card.blocked_by_admin = True
        card.save(update_fields=['status', 'blocked_by_admin', 'updated_at'])
        DebitCardAuditLog.objects.create(card=card, performed_by=request.user, action='BLOCKED_ADMIN', reason=reason)
        log_employee_activity(
            employee=request.user,
            action_type='CARD_STATUS_CHANGE',
            customer=card.user,
            account_number=card.account.account_number if card.account else '',
            target_id=str(card.id),
            description=f"Admin blocked virtual card #{card.id} ({card.masked_card_number}). Reason: {reason}",
            request=request
        )
        send_in_app_notification(
            recipient=card.user,
            notification_type='SECURITY',
            title="Virtual Debit Card Suspended",
            message=f"Your virtual debit card {card.masked_card_number} was suspended by bank administration. Reason: {reason}",
            target_url=f"/bank/cards/{card.id}/"
        )
        messages.success(request, f"Card #{card.id} ({card.masked_card_number}) administratively blocked ✅")

    elif action_type == 'unblock':
        card.status = 'ACTIVE'
        card.blocked_by_admin = False
        card.save(update_fields=['status', 'blocked_by_admin', 'updated_at'])
        DebitCardAuditLog.objects.create(card=card, performed_by=request.user, action='UNBLOCKED_ADMIN', reason=reason)
        log_employee_activity(
            employee=request.user,
            action_type='CARD_STATUS_CHANGE',
            customer=card.user,
            account_number=card.account.account_number if card.account else '',
            target_id=str(card.id),
            description=f"Admin unblocked virtual card #{card.id} ({card.masked_card_number}). Reason: {reason}",
            request=request
        )
        send_in_app_notification(
            recipient=card.user,
            notification_type='CARD',
            title="Virtual Debit Card Re-activated",
            message=f"Your virtual debit card {card.masked_card_number} was unblocked by bank administration.",
            target_url=f"/bank/cards/{card.id}/"
        )
        messages.success(request, f"Card #{card.id} ({card.masked_card_number}) unblocked by Admin ✅")

    elif action_type == 'cancel':
        card.status = 'CANCELLED'
        card.save(update_fields=['status', 'updated_at'])
        DebitCardAuditLog.objects.create(card=card, performed_by=request.user, action='CANCELLED_ADMIN', reason=reason)
        log_employee_activity(
            employee=request.user,
            action_type='CARD_STATUS_CHANGE',
            customer=card.user,
            account_number=card.account.account_number if card.account else '',
            target_id=str(card.id),
            description=f"Admin permanently cancelled virtual card #{card.id} ({card.masked_card_number}). Reason: {reason}",
            request=request
        )
        send_in_app_notification(
            recipient=card.user,
            notification_type='SECURITY',
            title="Virtual Debit Card Terminated",
            message=f"Your virtual debit card {card.masked_card_number} was permanently cancelled by bank administration. Reason: {reason}",
            target_url=f"/bank/cards/{card.id}/"
        )
        messages.success(request, f"Card #{card.id} ({card.masked_card_number}) permanently cancelled by Admin ✅")

    else:
        messages.error(request, "Invalid administrative action type ❌")

    if next_url:
        return redirect(next_url)
    return redirect('staff_card_detail', card_id=card.id)


# =========================
# ANALYTICS
# =========================
# =========================================================================
# EMPLOYEE REPORTS & ANALYTICS (TASK 3.4)
# =========================================================================
@role_required('EMPLOYEE', 'ADMIN')
def analytics_view(request):
    """
    Comprehensive database-backed Employee Reports & Analytics workspace.
    Supports date range filtering, operation breakdown, loan pipeline,
    FD and card summaries, support volume, and role-governed staff performance.
    """
    today = timezone.localdate()
    date_from_str = request.GET.get('date_from', '').strip()
    date_to_str = request.GET.get('date_to', '').strip()
    report_type = request.GET.get('report_type', 'overview').strip().lower()

    # Parse date range, defaulting to last 30 days
    try:
        date_from = datetime.strptime(date_from_str, '%Y-%m-%d').date() if date_from_str else (today - timedelta(days=30))
    except ValueError:
        date_from = today - timedelta(days=30)

    try:
        date_to = datetime.strptime(date_to_str, '%Y-%m-%d').date() if date_to_str else today
    except ValueError:
        date_to = today

    if date_from > date_to:
        date_from, date_to = date_to, date_from

    # Total database counters
    total_accounts = BankAccount.objects.count()
    total_balance = BankAccount.objects.aggregate(total=Sum('balance'))['total'] or Decimal('0.00')
    total_loans = Loan.objects.count()

    # 1. Deposits & Withdrawals in date range
    range_deposits = Transaction.objects.filter(
        transaction_type='DEPOSIT',
        created_at__date__gte=date_from,
        created_at__date__lte=date_to
    ).aggregate(total=Sum('amount'), count=Count('id'))
    deposit_volume = range_deposits['total'] or Decimal('0.00')
    deposit_count = range_deposits['count'] or 0

    range_withdrawals = Transaction.objects.filter(
        transaction_type='WITHDRAW',
        created_at__date__gte=date_from,
        created_at__date__lte=date_to
    ).aggregate(total=Sum('amount'), count=Count('id'))
    withdrawal_volume = range_withdrawals['total'] or Decimal('0.00')
    withdrawal_count = range_withdrawals['count'] or 0

    net_cash_flow = deposit_volume - withdrawal_volume

    # 2. Account Openings in date range
    new_accounts_count = BankAccount.objects.filter(
        created_at__date__gte=date_from,
        created_at__date__lte=date_to
    ).count()

    # 3. Transaction Volume breakdown by type in date range
    tx_type_breakdown = list(Transaction.objects.filter(
        created_at__date__gte=date_from,
        created_at__date__lte=date_to
    ).values('transaction_type').annotate(
        total_amount=Sum('amount'),
        tx_count=Count('id')
    ).order_by('-total_amount'))

    # 4. Daily transaction trend (for charts and tables)
    daily_tx_summary = list(Transaction.objects.filter(
        created_at__date__gte=date_from,
        created_at__date__lte=date_to,
        transaction_type__in=['DEPOSIT', 'WITHDRAW']
    ).values('created_at__date', 'transaction_type').annotate(
        total_amount=Sum('amount'),
        cnt=Count('id')
    ).order_by('-created_at__date'))

    daily_rows_map = {}
    for entry in daily_tx_summary:
        d = entry['created_at__date']
        if d not in daily_rows_map:
            daily_rows_map[d] = {'date': d, 'deposit': Decimal('0.00'), 'deposit_count': 0, 'withdraw': Decimal('0.00'), 'withdraw_count': 0}
        if entry['transaction_type'] == 'DEPOSIT':
            daily_rows_map[d]['deposit'] = entry['total_amount'] or Decimal('0.00')
            daily_rows_map[d]['deposit_count'] = entry['cnt']
        elif entry['transaction_type'] == 'WITHDRAW':
            daily_rows_map[d]['withdraw'] = entry['total_amount'] or Decimal('0.00')
            daily_rows_map[d]['withdraw_count'] = entry['cnt']

    daily_tx_list = sorted(daily_rows_map.values(), key=lambda x: x['date'], reverse=True)
    for row in daily_tx_list:
        row['net'] = row['deposit'] - row['withdraw']

    # 5. Loan Pipeline Summary
    loan_statuses = ['PENDING', 'UNDER_REVIEW', 'APPROVED', 'REJECTED', 'CLOSED']
    loan_pipeline = []
    for st in loan_statuses:
        agg = Loan.objects.filter(status=st, created_at__date__gte=date_from, created_at__date__lte=date_to).aggregate(
            cnt=Count('id'), total=Sum('amount')
        )
        loan_pipeline.append({
            'status': st,
            'count': agg['cnt'] or 0,
            'amount': agg['total'] or Decimal('0.00')
        })

    # 6. Fixed Deposits Summary
    fd_active_agg = FixedDeposit.objects.filter(status='ACTIVE').aggregate(cnt=Count('id'), total=Sum('amount'), maturity_total=Sum('maturity_amount'))
    fd_matured_agg = FixedDeposit.objects.filter(status='MATURED').aggregate(cnt=Count('id'), total=Sum('amount'))
    fd_closed_agg = FixedDeposit.objects.filter(status='CLOSED').aggregate(cnt=Count('id'), total=Sum('amount'))

    thirty_days_later = timezone.now() + timedelta(days=30)
    fd_upcoming_count = FixedDeposit.objects.filter(
        status='ACTIVE', maturity_date__gte=timezone.now(), maturity_date__lte=thirty_days_later
    ).count()

    fd_summary = {
        'active_count': fd_active_agg['cnt'] or 0,
        'active_principal': fd_active_agg['total'] or Decimal('0.00'),
        'active_maturity_liability': fd_active_agg['maturity_total'] or Decimal('0.00'),
        'matured_count': fd_matured_agg['cnt'] or 0,
        'matured_principal': fd_matured_agg['total'] or Decimal('0.00'),
        'closed_count': fd_closed_agg['cnt'] or 0,
        'closed_principal': fd_closed_agg['total'] or Decimal('0.00'),
        'upcoming_maturities_30d': fd_upcoming_count,
    }

    # 7. Virtual Debit Cards Summary
    cards_summary = {
        'total': VirtualDebitCard.objects.count(),
        'active': VirtualDebitCard.objects.filter(status='ACTIVE').count(),
        'blocked': VirtualDebitCard.objects.filter(status='BLOCKED').count(),
        'expired': VirtualDebitCard.objects.filter(status='EXPIRED').count(),
        'cancelled': VirtualDebitCard.objects.filter(status='CANCELLED').count(),
    }

    # 8. Customer Support Summary
    support_total = SupportTicket.objects.filter(created_at__date__gte=date_from, created_at__date__lte=date_to).count()
    support_resolved = SupportTicket.objects.filter(
        status__in=['RESOLVED', 'CLOSED'],
        updated_at__date__gte=date_from,
        updated_at__date__lte=date_to
    ).count()
    support_open = SupportTicket.objects.filter(status='OPEN').count()
    support_in_progress = SupportTicket.objects.filter(status='IN_PROGRESS').count()
    support_waiting = SupportTicket.objects.filter(status='WAITING_FOR_CUSTOMER').count()

    support_summary = {
        'total_in_period': support_total,
        'resolved_in_period': support_resolved,
        'open_now': support_open,
        'in_progress_now': support_in_progress,
        'waiting_now': support_waiting,
        'resolution_rate': round((support_resolved / support_total * 100), 1) if support_total > 0 else 100.0,
    }

    # 9. Employee Performance Summary (Strict Access Control)
    staff_performance = []
    target_staff_qs = User.objects.filter(role__in=['EMPLOYEE', 'ADMIN']).order_by('role', 'username')

    selected_emp_id = request.GET.get('employee_id', '').strip()
    if request.user.role == 'EMPLOYEE':
        target_staff_qs = target_staff_qs.filter(id=request.user.id)
    elif selected_emp_id:
        target_staff_qs = target_staff_qs.filter(id=selected_emp_id)

    for staff in target_staff_qs:
        t_ops = TellerOperationLog.objects.filter(
            employee=staff, created_at__date__gte=date_from, created_at__date__lte=date_to
        ).aggregate(cnt=Count('id'), total=Sum('amount'))
        k_cnt = User.objects.filter(
            kyc_reviewed_by=staff, kyc_reviewed_at__date__gte=date_from, kyc_reviewed_at__date__lte=date_to
        ).count()
        l_cnt = LoanVerificationLog.objects.filter(
            performed_by=staff, created_at__date__gte=date_from, created_at__date__lte=date_to
        ).count()
        tkt_cnt = SupportTicket.objects.filter(
            assigned_to=staff, status__in=['RESOLVED', 'CLOSED'], updated_at__date__gte=date_from, updated_at__date__lte=date_to
        ).count()

        staff_performance.append({
            'user': staff,
            'teller_ops_count': t_ops['cnt'] or 0,
            'teller_volume': t_ops['total'] or Decimal('0.00'),
            'kyc_reviews': k_cnt,
            'loan_verifications': l_cnt,
            'tickets_resolved': tkt_cnt,
        })

    log_employee_activity(
        employee=request.user,
        action_type='REPORT_VIEW',
        description=f"Viewed {report_type} reports ({date_from} to {date_to})",
        request=request
    )

    return render(request, "analytics.html", {
        # Legacy keys for backward compatibility
        "accounts": total_accounts,
        "balance": float(total_balance),
        "loans": total_loans,
        # Enhanced report data
        "report_type": report_type,
        "date_from": date_from.strftime('%Y-%m-%d'),
        "date_to": date_to.strftime('%Y-%m-%d'),
        "deposit_volume": deposit_volume,
        "deposit_count": deposit_count,
        "withdrawal_volume": withdrawal_volume,
        "withdrawal_count": withdrawal_count,
        "net_cash_flow": net_cash_flow,
        "new_accounts_count": new_accounts_count,
        "tx_type_breakdown": tx_type_breakdown,
        "daily_tx_list": daily_tx_list,
        "loan_pipeline": loan_pipeline,
        "fd_summary": fd_summary,
        "cards_summary": cards_summary,
        "support_summary": support_summary,
        "staff_performance": staff_performance,
        "staff_members": User.objects.filter(role__in=['EMPLOYEE', 'ADMIN']).order_by('role', 'username') if request.user.role == 'ADMIN' else [],
        "selected_emp_id": selected_emp_id if request.user.role == 'ADMIN' else str(request.user.id),
        "is_admin": request.user.role == 'ADMIN',
    })


@role_required('EMPLOYEE', 'ADMIN')
def staff_reports_export_csv_view(request):
    """
    Exports filtered report data to CSV with safe formula injection protection.
    Respects role authorization: employees can only export authorized performance data.
    """
    today = timezone.localdate()
    date_from_str = request.GET.get('date_from', '').strip()
    date_to_str = request.GET.get('date_to', '').strip()
    report_type = request.GET.get('report_type', 'overview').strip().lower()

    try:
        date_from = datetime.strptime(date_from_str, '%Y-%m-%d').date() if date_from_str else (today - timedelta(days=30))
    except ValueError:
        date_from = today - timedelta(days=30)

    try:
        date_to = datetime.strptime(date_to_str, '%Y-%m-%d').date() if date_to_str else today
    except ValueError:
        date_to = today

    if date_from > date_to:
        date_from, date_to = date_to, date_from

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="bebank_report_{report_type}_{date_from}_{date_to}.csv"'
    writer = csv.writer(response)

    if report_type == 'transactions':
        writer.writerow([
            safe_csv_cell("Date"),
            safe_csv_cell("Total Deposit Amount (INR)"),
            safe_csv_cell("Deposit Count"),
            safe_csv_cell("Total Withdrawal Amount (INR)"),
            safe_csv_cell("Withdrawal Count"),
            safe_csv_cell("Net Counter Cash Flow (INR)"),
        ])
        daily_tx = list(Transaction.objects.filter(
            created_at__date__gte=date_from,
            created_at__date__lte=date_to,
            transaction_type__in=['DEPOSIT', 'WITHDRAW']
        ).values('created_at__date', 'transaction_type').annotate(
            total_amount=Sum('amount'), cnt=Count('id')
        ).order_by('-created_at__date'))

        d_map = {}
        for entry in daily_tx:
            d = entry['created_at__date']
            if d not in d_map:
                d_map[d] = {'date': d, 'dep': Decimal('0.00'), 'dep_c': 0, 'wdr': Decimal('0.00'), 'wdr_c': 0}
            if entry['transaction_type'] == 'DEPOSIT':
                d_map[d]['dep'] = entry['total_amount'] or Decimal('0.00')
                d_map[d]['dep_c'] = entry['cnt']
            elif entry['transaction_type'] == 'WITHDRAW':
                d_map[d]['wdr'] = entry['total_amount'] or Decimal('0.00')
                d_map[d]['wdr_c'] = entry['cnt']

        for row in sorted(d_map.values(), key=lambda x: x['date'], reverse=True):
            net = row['dep'] - row['wdr']
            writer.writerow([
                safe_csv_cell(row['date'].strftime('%Y-%m-%d')),
                safe_csv_cell(f"{row['dep']:.2f}"),
                safe_csv_cell(row['dep_c']),
                safe_csv_cell(f"{row['wdr']:.2f}"),
                safe_csv_cell(row['wdr_c']),
                safe_csv_cell(f"{net:.2f}"),
            ])

    elif report_type == 'performance':
        writer.writerow([
            safe_csv_cell("Staff Username"),
            safe_csv_cell("Role"),
            safe_csv_cell("Teller Operations Count"),
            safe_csv_cell("Teller Transaction Volume (INR)"),
            safe_csv_cell("KYC Reviews Completed"),
            safe_csv_cell("Loan Underwritings"),
            safe_csv_cell("Support Tickets Resolved"),
        ])
        target_staff = User.objects.filter(role__in=['EMPLOYEE', 'ADMIN']).order_by('role', 'username')
        if request.user.role == 'EMPLOYEE':
            target_staff = target_staff.filter(id=request.user.id)
        elif request.GET.get('employee_id'):
            target_staff = target_staff.filter(id=request.GET.get('employee_id'))

        for staff in target_staff:
            t_ops = TellerOperationLog.objects.filter(
                employee=staff, created_at__date__gte=date_from, created_at__date__lte=date_to
            ).aggregate(cnt=Count('id'), total=Sum('amount'))
            k_cnt = User.objects.filter(
                kyc_reviewed_by=staff, kyc_reviewed_at__date__gte=date_from, kyc_reviewed_at__date__lte=date_to
            ).count()
            l_cnt = LoanVerificationLog.objects.filter(
                performed_by=staff, created_at__date__gte=date_from, created_at__date__lte=date_to
            ).count()
            tkt_cnt = SupportTicket.objects.filter(
                assigned_to=staff, status__in=['RESOLVED', 'CLOSED'], updated_at__date__gte=date_from, updated_at__date__lte=date_to
            ).count()

            writer.writerow([
                safe_csv_cell(staff.username),
                safe_csv_cell(staff.role),
                safe_csv_cell(t_ops['cnt'] or 0),
                safe_csv_cell(f"{t_ops['total'] or Decimal('0.00'):.2f}"),
                safe_csv_cell(k_cnt),
                safe_csv_cell(l_cnt),
                safe_csv_cell(tkt_cnt),
            ])

    else:
        # Overview report
        writer.writerow([safe_csv_cell("Metric"), safe_csv_cell("Value")])
        writer.writerow([safe_csv_cell("Reporting Period From"), safe_csv_cell(date_from.strftime('%Y-%m-%d'))])
        writer.writerow([safe_csv_cell("Reporting Period To"), safe_csv_cell(date_to.strftime('%Y-%m-%d'))])

        dep = Transaction.objects.filter(transaction_type='DEPOSIT', created_at__date__gte=date_from, created_at__date__lte=date_to).aggregate(total=Sum('amount'), cnt=Count('id'))
        wdr = Transaction.objects.filter(transaction_type='WITHDRAW', created_at__date__gte=date_from, created_at__date__lte=date_to).aggregate(total=Sum('amount'), cnt=Count('id'))
        d_val = dep['total'] or Decimal('0.00')
        w_val = wdr['total'] or Decimal('0.00')

        writer.writerow([safe_csv_cell("Deposits Volume (INR)"), safe_csv_cell(f"{d_val:.2f}")])
        writer.writerow([safe_csv_cell("Deposits Count"), safe_csv_cell(dep['cnt'] or 0)])
        writer.writerow([safe_csv_cell("Withdrawals Volume (INR)"), safe_csv_cell(f"{w_val:.2f}")])
        writer.writerow([safe_csv_cell("Withdrawals Count"), safe_csv_cell(wdr['cnt'] or 0)])
        writer.writerow([safe_csv_cell("Net Counter Cash Flow (INR)"), safe_csv_cell(f"{d_val - w_val:.2f}")])
        writer.writerow([safe_csv_cell("New Bank Accounts Opened"), safe_csv_cell(BankAccount.objects.filter(created_at__date__gte=date_from, created_at__date__lte=date_to).count())])
        writer.writerow([safe_csv_cell("Active Accounts (Total)"), safe_csv_cell(BankAccount.objects.filter(user__account_locked=False).count())])
        writer.writerow([safe_csv_cell("Total Customer Deposits Ledger (INR)"), safe_csv_cell(f"{BankAccount.objects.aggregate(total=Sum('balance'))['total'] or Decimal('0.00'):.2f}")])
        writer.writerow([safe_csv_cell("Active Loans Count"), safe_csv_cell(Loan.objects.filter(status='APPROVED').count())])
        writer.writerow([safe_csv_cell("Active Fixed Deposits Count"), safe_csv_cell(FixedDeposit.objects.filter(status='ACTIVE').count())])
        writer.writerow([safe_csv_cell("Active Debit Cards Count"), safe_csv_cell(VirtualDebitCard.objects.filter(status='ACTIVE').count())])
        writer.writerow([safe_csv_cell("Support Tickets Resolved in Period"), safe_csv_cell(SupportTicket.objects.filter(status__in=['RESOLVED', 'CLOSED'], updated_at__date__gte=date_from, updated_at__date__lte=date_to).count())])

    log_employee_activity(
        employee=request.user,
        action_type='REPORT_EXPORT',
        description=f"Exported {report_type} report CSV ({date_from} to {date_to})",
        request=request
    )

    return response


# =========================================================================
# EMPLOYEE ACTIVITY & AUDIT LOG VIEWS (TASK 3.4)
# =========================================================================
@role_required('EMPLOYEE', 'ADMIN')
def staff_activity_log_view(request):
    """
    Employee activity and audit log surveillance workspace.
    Employees see only their own actions.
    Admins see all staff actions or can filter by specific employee.
    Supports filtering by action type, date range, search query, and pagination.
    """
    base_qs = EmployeeActivityLog.objects.select_related('employee', 'customer').all().order_by('-created_at')

    if request.user.role == 'EMPLOYEE':
        base_qs = base_qs.filter(employee=request.user)
        staff_members = []
    else:
        staff_members = User.objects.filter(role__in=['EMPLOYEE', 'ADMIN']).order_by('role', 'username')

    selected_employee = request.GET.get('employee', '').strip()
    selected_action = request.GET.get('action_type', '').strip().upper()
    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()
    query = request.GET.get('q', '').strip()

    qs = base_qs
    if request.user.role == 'ADMIN' and selected_employee:
        qs = qs.filter(employee__id=selected_employee)

    if selected_action in dict(EmployeeActivityLog.ACTION_CHOICES):
        qs = qs.filter(action_type=selected_action)
    else:
        selected_action = ''

    if date_from:
        try:
            d_from = datetime.strptime(date_from, '%Y-%m-%d').date()
            qs = qs.filter(created_at__date__gte=d_from)
        except ValueError:
            date_from = ''

    if date_to:
        try:
            d_to = datetime.strptime(date_to, '%Y-%m-%d').date()
            qs = qs.filter(created_at__date__lte=d_to)
        except ValueError:
            date_to = ''

    if query:
        qs = qs.filter(
            Q(description__icontains=query) |
            Q(account_number__icontains=query) |
            Q(target_id__icontains=query) |
            Q(customer__username__icontains=query) |
            Q(employee__username__icontains=query)
        )

    paginator = Paginator(qs, 25)
    page_number = request.GET.get('page', 1)
    try:
        page_obj = paginator.page(page_number)
    except (PageNotAnInteger, EmptyPage):
        page_obj = paginator.page(1)

    return render(request, "staff_activity_log.html", {
        'page_obj': page_obj,
        'logs': page_obj,
        'selected_action': selected_action,
        'selected_employee': selected_employee,
        'date_from': date_from,
        'date_to': date_to,
        'search_query': query,
        'action_choices': EmployeeActivityLog.ACTION_CHOICES,
        'staff_members': staff_members,
        'is_admin': request.user.role == 'ADMIN',
    })


@role_required('EMPLOYEE', 'ADMIN')
def staff_activity_export_csv_view(request):
    """Exports filtered employee activity logs to CSV with formula injection prevention."""
    base_qs = EmployeeActivityLog.objects.select_related('employee', 'customer').all().order_by('-created_at')

    if request.user.role == 'EMPLOYEE':
        base_qs = base_qs.filter(employee=request.user)
    elif request.user.role == 'ADMIN':
        selected_employee = request.GET.get('employee', '').strip()
        if selected_employee:
            base_qs = base_qs.filter(employee__id=selected_employee)

    selected_action = request.GET.get('action_type', '').strip().upper()
    if selected_action in dict(EmployeeActivityLog.ACTION_CHOICES):
        base_qs = base_qs.filter(action_type=selected_action)

    date_from = request.GET.get('date_from', '').strip()
    if date_from:
        try:
            base_qs = base_qs.filter(created_at__date__gte=datetime.strptime(date_from, '%Y-%m-%d').date())
        except ValueError:
            pass

    date_to = request.GET.get('date_to', '').strip()
    if date_to:
        try:
            base_qs = base_qs.filter(created_at__date__lte=datetime.strptime(date_to, '%Y-%m-%d').date())
        except ValueError:
            pass

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    timestamp_str = timezone.now().strftime('%Y%m%d_%H%M%S')
    response['Content-Disposition'] = f'attachment; filename="bebank_activity_audit_{timestamp_str}.csv"'

    writer = csv.writer(response)
    writer.writerow([
        safe_csv_cell("Timestamp"),
        safe_csv_cell("Actor Username"),
        safe_csv_cell("Role"),
        safe_csv_cell("Action Type"),
        safe_csv_cell("Customer"),
        safe_csv_cell("Account Number"),
        safe_csv_cell("Target ID"),
        safe_csv_cell("Description"),
        safe_csv_cell("IP Address"),
    ])

    for log in base_qs.iterator(chunk_size=500):
        writer.writerow([
            safe_csv_cell(log.created_at.strftime('%Y-%m-%d %H:%M:%S')),
            safe_csv_cell(log.employee.username if log.employee else 'System'),
            safe_csv_cell(log.employee.role if log.employee else ''),
            safe_csv_cell(log.action_type),
            safe_csv_cell(log.customer.username if log.customer else ''),
            safe_csv_cell(log.account_number),
            safe_csv_cell(log.target_id),
            safe_csv_cell(log.description),
            safe_csv_cell(log.ip_address or ''),
        ])

    log_employee_activity(
        employee=request.user,
        action_type='REPORT_EXPORT',
        description="Exported Employee Activity Audit CSV",
        request=request
    )

    return response


# =========================
# NOTIFICATIONS (IN-APP PERSISTENT)
# =========================
@role_required('CUSTOMER', 'EMPLOYEE', 'ADMIN')
def notification_list_view(request):
    """Notification inbox with filtering and pagination."""
    base_qs = Notification.objects.filter(recipient=request.user)
    unread_count = base_qs.filter(is_read=False).count()

    selected_status = request.GET.get('status', '').strip().lower()
    selected_type = request.GET.get('type', '').strip().upper()

    qs = base_qs
    if selected_status == 'unread':
        qs = qs.filter(is_read=False)
    elif selected_status == 'read':
        qs = qs.filter(is_read=True)

    if selected_type in dict(Notification.NOTIFICATION_TYPES):
        qs = qs.filter(notification_type=selected_type)
    else:
        selected_type = ''

    paginator = Paginator(qs, 20)
    page_number = request.GET.get('page', 1)
    try:
        page_obj = paginator.page(page_number)
    except (PageNotAnInteger, EmptyPage):
        page_obj = paginator.page(1)

    return render(request, "notifications.html", {
        'notifications': page_obj,
        'page_obj': page_obj,
        'unread_count': unread_count,
        'selected_status': selected_status,
        'selected_type': selected_type,
        'type_choices': Notification.NOTIFICATION_TYPES,
    })


@require_POST
@role_required('CUSTOMER', 'EMPLOYEE', 'ADMIN')
def notification_mark_read_view(request, notification_id):
    """Mark a single notification as read with strict ownership check."""
    notif = get_object_or_404(Notification, id=notification_id, recipient=request.user)
    notif.mark_as_read()

    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        unread = Notification.objects.filter(recipient=request.user, is_read=False).count()
        return JsonResponse({'success': True, 'unread_count': unread})

    if request.POST.get('navigate') == '1' and notif.safe_target_url:
        return redirect(notif.safe_target_url)

    next_url = request.POST.get('next') or request.META.get('HTTP_REFERER') or '/bank/notifications/'
    return redirect(next_url)


@require_POST
@role_required('CUSTOMER', 'EMPLOYEE', 'ADMIN')
def notification_mark_all_read_view(request):
    """Mark all unread notifications for current user as read."""
    Notification.objects.filter(recipient=request.user, is_read=False).update(
        is_read=True, read_at=timezone.now()
    )

    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        return JsonResponse({'success': True})

    messages.success(request, "All notifications marked as read ✅")
    return redirect('notifications_list')


# =========================
# CUSTOMER SUPPORT & TICKETS
# =========================
@role_required('CUSTOMER')
def support_ticket_list_view(request):
    """Customer view to browse their own support tickets."""
    base_qs = SupportTicket.objects.filter(customer=request.user).order_by('-updated_at')

    status_filter = request.GET.get('status', '').strip().upper()
    category_filter = request.GET.get('category', '').strip().upper()

    qs = base_qs
    if status_filter in dict(SupportTicket.STATUS_CHOICES):
        qs = qs.filter(status=status_filter)
    else:
        status_filter = ''

    if category_filter in dict(SupportTicket.CATEGORY_CHOICES):
        qs = qs.filter(category=category_filter)
    else:
        category_filter = ''

    paginator = Paginator(qs, 15)
    page_number = request.GET.get('page', 1)
    try:
        page_obj = paginator.page(page_number)
    except (PageNotAnInteger, EmptyPage):
        page_obj = paginator.page(1)

    open_count = base_qs.filter(status__in=['OPEN', 'IN_PROGRESS', 'WAITING_FOR_CUSTOMER']).count()

    return render(request, "support_list.html", {
        'tickets': page_obj,
        'page_obj': page_obj,
        'selected_status': status_filter,
        'selected_category': category_filter,
        'status_choices': SupportTicket.STATUS_CHOICES,
        'category_choices': SupportTicket.CATEGORY_CHOICES,
        'open_tickets_count': open_count,
    })


@role_required('CUSTOMER')
def support_ticket_create_view(request):
    """Customer opens a new support ticket / complaint."""
    if request.method == 'POST':
        category = request.POST.get('category', '').strip().upper()
        priority = request.POST.get('priority', 'MEDIUM').strip().upper()
        subject = request.POST.get('subject', '').strip()
        description = request.POST.get('description', '').strip()

        if category not in dict(SupportTicket.CATEGORY_CHOICES):
            messages.error(request, "Please select a valid ticket category ❌")
            return redirect('support_ticket_create')

        if priority not in dict(SupportTicket.PRIORITY_CHOICES):
            priority = 'MEDIUM'

        if len(subject) < 4 or len(subject) > 150:
            messages.error(request, "Subject must be between 4 and 150 characters ❌")
            return redirect('support_ticket_create')

        if len(description) < 10:
            messages.error(request, "Please describe your issue in detail (at least 10 characters) ❌")
            return redirect('support_ticket_create')

        ticket_number = generate_ticket_number()
        while SupportTicket.objects.filter(ticket_number=ticket_number).exists():
            ticket_number = generate_ticket_number()

        with transaction.atomic():
            ticket = SupportTicket.objects.create(
                ticket_number=ticket_number,
                customer=request.user,
                category=category,
                subject=subject,
                description=description,
                priority=priority,
                status='OPEN'
            )
            SupportMessage.objects.create(
                ticket=ticket,
                author=request.user,
                message=description,
                is_internal_note=False
            )

        send_in_app_notification(
            recipient=request.user,
            notification_type='SUPPORT',
            title=f"Support Ticket Created: {ticket.ticket_number}",
            message=f"Your ticket \"{ticket.subject}\" has been submitted. Reference: {ticket.ticket_number}",
            target_url=f"/bank/support/{ticket.id}/"
        )

        messages.success(request, f"Support ticket #{ticket.ticket_number} created successfully! Our team will assist you shortly ✅")
        return redirect('support_ticket_detail', ticket_id=ticket.id)

    return render(request, "support_create.html", {
        'category_choices': SupportTicket.CATEGORY_CHOICES,
        'priority_choices': SupportTicket.PRIORITY_CHOICES,
    })


@role_required('CUSTOMER')
def support_ticket_detail_view(request, ticket_id):
    """Customer views their ticket and public conversation (internal notes strictly hidden)."""
    ticket = get_object_or_404(SupportTicket, id=ticket_id, customer=request.user)

    # CRITICAL SECURITY: Customers must NEVER see internal employee notes
    messages_list = ticket.messages.filter(is_internal_note=False).select_related('author').order_by('created_at')

    return render(request, "support_detail.html", {
        'ticket': ticket,
        'messages_list': messages_list,
        'can_reply': ticket.can_customer_reply(),
        'can_reopen': ticket.can_customer_reopen(),
    })


@require_POST
@role_required('CUSTOMER')
def support_ticket_reply_view(request, ticket_id):
    """Customer replies to an open support ticket."""
    ticket = get_object_or_404(SupportTicket, id=ticket_id, customer=request.user)

    if not ticket.can_customer_reply():
        messages.error(request, "This ticket is closed or resolved. Reopen the ticket to continue conversation ❌")
        return redirect('support_ticket_detail', ticket_id=ticket.id)

    reply_text = request.POST.get('message', '').strip()
    if len(reply_text) < 2:
        messages.error(request, "Please enter a message before sending ❌")
        return redirect('support_ticket_detail', ticket_id=ticket.id)

    with transaction.atomic():
        SupportMessage.objects.create(
            ticket=ticket,
            author=request.user,
            message=reply_text,
            is_internal_note=False
        )
        if ticket.status == 'WAITING_FOR_CUSTOMER':
            ticket.status = 'IN_PROGRESS'
        ticket.save(update_fields=['status', 'updated_at'])

    if ticket.assigned_to:
        send_in_app_notification(
            recipient=ticket.assigned_to,
            notification_type='SUPPORT',
            title=f"Customer Replied: {ticket.ticket_number}",
            message=f"Customer {request.user.username} sent a reply on ticket #{ticket.ticket_number}.",
            target_url=f"/bank/staff/support/{ticket.id}/"
        )

    messages.success(request, "Your reply has been sent ✅")
    return redirect('support_ticket_detail', ticket_id=ticket.id)


@require_POST
@role_required('CUSTOMER')
def support_ticket_reopen_view(request, ticket_id):
    """Customer reopens a resolved support ticket."""
    ticket = get_object_or_404(SupportTicket, id=ticket_id, customer=request.user)

    if not ticket.can_customer_reopen():
        messages.error(request, "Only resolved tickets can be reopened ❌")
        return redirect('support_ticket_detail', ticket_id=ticket.id)

    reason = request.POST.get('reason', '').strip() or "Customer reopened this ticket for further assistance."

    with transaction.atomic():
        ticket.status = 'OPEN'
        ticket.resolved_at = None
        ticket.save(update_fields=['status', 'resolved_at', 'updated_at'])

        SupportMessage.objects.create(
            ticket=ticket,
            author=request.user,
            message=f"[Customer Reopened Ticket] {reason}",
            is_internal_note=False
        )

    if ticket.assigned_to:
        send_in_app_notification(
            recipient=ticket.assigned_to,
            notification_type='SUPPORT',
            title=f"Ticket Reopened: {ticket.ticket_number}",
            message=f"Customer {request.user.username} reopened ticket #{ticket.ticket_number}.",
            target_url=f"/bank/staff/support/{ticket.id}/"
        )

    messages.success(request, f"Ticket #{ticket.ticket_number} has been reopened ✅")
    return redirect('support_ticket_detail', ticket_id=ticket.id)


# =========================
# STAFF SUPPORT QUEUE & DETAIL
# =========================
@role_required('EMPLOYEE', 'ADMIN')
def staff_support_queue_view(request):
    """Staff support ticket workspace with filtering, search, and metrics."""
    base_qs = SupportTicket.objects.select_related('customer', 'assigned_to').all().order_by('-updated_at')

    open_count = SupportTicket.objects.filter(status='OPEN').count()
    in_progress_count = SupportTicket.objects.filter(status='IN_PROGRESS').count()
    waiting_count = SupportTicket.objects.filter(status='WAITING_FOR_CUSTOMER').count()
    resolved_count = SupportTicket.objects.filter(status='RESOLVED').count()
    closed_count = SupportTicket.objects.filter(status='CLOSED').count()
    unassigned_count = SupportTicket.objects.filter(assigned_to__isnull=True).count()
    assigned_to_me_count = SupportTicket.objects.filter(
        assigned_to=request.user, status__in=['OPEN', 'IN_PROGRESS', 'WAITING_FOR_CUSTOMER']
    ).count()

    assigned_filter = request.GET.get('assigned', '').strip().lower()
    status_filter = request.GET.get('status', '').strip().upper()
    category_filter = request.GET.get('category', '').strip().upper()
    priority_filter = request.GET.get('priority', '').strip().upper()
    query = request.GET.get('q', '').strip()

    qs = base_qs
    if assigned_filter == 'me':
        qs = qs.filter(assigned_to=request.user)
    elif assigned_filter == 'unassigned':
        qs = qs.filter(assigned_to__isnull=True)

    if status_filter in dict(SupportTicket.STATUS_CHOICES):
        qs = qs.filter(status=status_filter)
    else:
        status_filter = ''

    if category_filter in dict(SupportTicket.CATEGORY_CHOICES):
        qs = qs.filter(category=category_filter)
    else:
        category_filter = ''

    if priority_filter in dict(SupportTicket.PRIORITY_CHOICES):
        qs = qs.filter(priority=priority_filter)
    else:
        priority_filter = ''

    if query:
        qs = qs.filter(
            Q(ticket_number__icontains=query) |
            Q(subject__icontains=query) |
            Q(customer__username__icontains=query) |
            Q(customer__first_name__icontains=query) |
            Q(customer__last_name__icontains=query) |
            Q(customer__email__icontains=query) |
            Q(customer__phone__icontains=query)
        ).distinct()

    paginator = Paginator(qs, 20)
    page_number = request.GET.get('page', 1)
    try:
        page_obj = paginator.page(page_number)
    except (PageNotAnInteger, EmptyPage):
        page_obj = paginator.page(1)

    return render(request, "staff_support_queue.html", {
        'tickets': page_obj,
        'page_obj': page_obj,
        'open_count': open_count,
        'in_progress_count': in_progress_count,
        'waiting_count': waiting_count,
        'resolved_count': resolved_count,
        'closed_count': closed_count,
        'unassigned_count': unassigned_count,
        'assigned_to_me_count': assigned_to_me_count,
        'selected_status': status_filter,
        'selected_category': category_filter,
        'selected_priority': priority_filter,
        'selected_assigned': assigned_filter,
        'search_query': query,
        'status_choices': SupportTicket.STATUS_CHOICES,
        'category_choices': SupportTicket.CATEGORY_CHOICES,
        'priority_choices': SupportTicket.PRIORITY_CHOICES,
    })


@role_required('EMPLOYEE', 'ADMIN')
def staff_support_detail_view(request, ticket_id):
    """Staff ticket detail view showing all customer communications and internal notes."""
    ticket = get_object_or_404(SupportTicket.objects.select_related('customer', 'assigned_to'), id=ticket_id)
    messages_list = ticket.messages.select_related('author').all().order_by('created_at')

    # Customer accounts overview for banking context
    customer_accounts = BankAccount.objects.filter(user=ticket.customer).order_by('-balance')
    primary_account = customer_accounts.first() if customer_accounts.exists() else None
    total_customer_balance = sum((acc.balance for acc in customer_accounts), Decimal('0.00'))

    staff_members = []
    if request.user.role == 'ADMIN':
        staff_members = User.objects.filter(role__in=['EMPLOYEE', 'ADMIN'], is_active=True).order_by('role', 'username')

    return render(request, "staff_support_detail.html", {
        'ticket': ticket,
        'messages_list': messages_list,
        'primary_account': primary_account,
        'total_customer_balance': total_customer_balance,
        'status_choices': SupportTicket.STATUS_CHOICES,
        'priority_choices': SupportTicket.PRIORITY_CHOICES,
        'staff_members': staff_members,
        'is_admin': request.user.role == 'ADMIN',
    })


@require_POST
@role_required('EMPLOYEE', 'ADMIN')
def staff_support_reply_view(request, ticket_id):
    """Staff adds a public reply or internal note, and optionally updates ticket status/priority."""
    ticket = get_object_or_404(SupportTicket, id=ticket_id)

    reply_text = request.POST.get('message', '').strip()
    is_internal = request.POST.get('is_internal_note') in ['1', 'true', 'on', True]
    status_update = request.POST.get('status', '').strip().upper()
    priority_update = request.POST.get('priority', '').strip().upper()

    with transaction.atomic():
        if reply_text:
            msg = SupportMessage.objects.create(
                ticket=ticket,
                author=request.user,
                message=reply_text,
                is_internal_note=is_internal
            )
            if is_internal:
                log_employee_activity(
                    employee=request.user,
                    action_type='SUPPORT_NOTE',
                    customer=ticket.customer,
                    target_id=ticket.ticket_number,
                    description=f"Added internal note on ticket #{ticket.ticket_number}",
                    changes=reply_text[:200],
                    request=request
                )
            else:
                log_employee_activity(
                    employee=request.user,
                    action_type='SUPPORT_REPLY',
                    customer=ticket.customer,
                    target_id=ticket.ticket_number,
                    description=f"Sent public reply to customer on ticket #{ticket.ticket_number}",
                    changes=reply_text[:200],
                    request=request
                )
                # Notify customer if reply is NOT internal note with deduplication protection
                send_in_app_notification(
                    recipient=ticket.customer,
                    notification_type='SUPPORT',
                    title=f"Support Update: {ticket.ticket_number}",
                    message=f"A bank representative replied to your ticket \"{ticket.subject}\".",
                    target_url=f"/bank/support/{ticket.id}/",
                    dedup_key=f"support_reply_{ticket.id}_{msg.id}"
                )

        if status_update in dict(SupportTicket.STATUS_CHOICES) and status_update != ticket.status:
            old_status = ticket.status
            ticket.status = status_update
            if status_update == 'RESOLVED':
                ticket.resolved_at = timezone.now()
            elif status_update == 'CLOSED':
                ticket.closed_at = timezone.now()
            elif old_status in ['RESOLVED', 'CLOSED'] and status_update in ['OPEN', 'IN_PROGRESS', 'WAITING_FOR_CUSTOMER']:
                ticket.resolved_at = None
                ticket.closed_at = None
            ticket.save(update_fields=['status', 'resolved_at', 'closed_at', 'updated_at'])

            log_employee_activity(
                employee=request.user,
                action_type='SUPPORT_STATUS_CHANGE',
                customer=ticket.customer,
                target_id=ticket.ticket_number,
                description=f"Changed ticket #{ticket.ticket_number} status from {old_status} to {status_update}",
                changes=f"Status: {old_status} -> {status_update}",
                request=request
            )

            send_in_app_notification(
                recipient=ticket.customer,
                notification_type='SUPPORT',
                title=f"Ticket Status Updated: {ticket.ticket_number}",
                message=f"Your ticket #{ticket.ticket_number} status has been updated to {dict(SupportTicket.STATUS_CHOICES).get(status_update)}.",
                target_url=f"/bank/support/{ticket.id}/",
                dedup_key=f"support_status_{ticket.id}_{status_update}_{int(timezone.now().timestamp() // 60)}"
            )

        if priority_update in dict(SupportTicket.PRIORITY_CHOICES) and priority_update != ticket.priority:
            ticket.priority = priority_update
            ticket.save(update_fields=['priority', 'updated_at'])

    messages.success(request, f"Ticket #{ticket.ticket_number} updated successfully ✅")
    return redirect('staff_support_detail', ticket_id=ticket.id)


@require_POST
@role_required('EMPLOYEE', 'ADMIN')
def staff_support_assign_view(request, ticket_id):
    """
    Ticket assignment endpoint with strict role boundaries:
    - ADMIN can assign to any active staff user (EMPLOYEE or ADMIN) or clear assignment.
    - EMPLOYEE can only assign to themselves ('claim') or release if currently assigned to them.
    - Any attempt by an EMPLOYEE to assign to an arbitrary user ID is strictly rejected with 403 Forbidden.
    """
    ticket = get_object_or_404(SupportTicket, id=ticket_id)
    action = request.POST.get('action', '').strip().lower()
    assigned_to_id = request.POST.get('assigned_to', '').strip()

    if request.user.role == 'ADMIN':
        if assigned_to_id:
            staff_user = get_object_or_404(User, id=assigned_to_id, role__in=['EMPLOYEE', 'ADMIN'], is_active=True)
            ticket.assigned_to = staff_user
            ticket.save(update_fields=['assigned_to', 'updated_at'])
            log_employee_activity(
                employee=request.user,
                action_type='SUPPORT_ASSIGNMENT',
                customer=ticket.customer,
                target_id=ticket.ticket_number,
                description=f"Admin assigned ticket #{ticket.ticket_number} to {staff_user.username}",
                request=request
            )
            send_in_app_notification(
                recipient=staff_user,
                notification_type='SUPPORT',
                title=f"Ticket Assigned: {ticket.ticket_number}",
                message=f"You have been assigned support ticket {ticket.ticket_number} ({ticket.subject}).",
                target_url=f"/bank/staff/support/{ticket.id}/",
                dedup_key=f"support_assign_{ticket.id}_{staff_user.id}"
            )
            messages.success(request, f"Ticket #{ticket.ticket_number} assigned to {staff_user.username} ✅")
        else:
            ticket.assigned_to = None
            ticket.save(update_fields=['assigned_to', 'updated_at'])
            log_employee_activity(
                employee=request.user,
                action_type='SUPPORT_ASSIGNMENT',
                customer=ticket.customer,
                target_id=ticket.ticket_number,
                description=f"Admin cleared assignment for ticket #{ticket.ticket_number}",
                request=request
            )
            messages.success(request, f"Ticket #{ticket.ticket_number} assignment cleared ✅")

    elif request.user.role == 'EMPLOYEE':
        # If assigned_to_id was provided and it's NOT the current employee, reject with 403!
        if assigned_to_id and str(assigned_to_id) != str(request.user.id):
            return HttpResponseForbidden("Employees are not authorized to assign tickets to arbitrary staff members.")

        if action == 'claim' or (assigned_to_id and str(assigned_to_id) == str(request.user.id)):
            ticket.assigned_to = request.user
            ticket.save(update_fields=['assigned_to', 'updated_at'])
            log_employee_activity(
                employee=request.user,
                action_type='SUPPORT_ASSIGNMENT',
                customer=ticket.customer,
                target_id=ticket.ticket_number,
                description=f"Claimed ticket #{ticket.ticket_number}",
                request=request
            )
            messages.success(request, f"Ticket #{ticket.ticket_number} assigned to you ✅")
        elif action == 'release':
            if ticket.assigned_to == request.user:
                ticket.assigned_to = None
                ticket.save(update_fields=['assigned_to', 'updated_at'])
                log_employee_activity(
                    employee=request.user,
                    action_type='SUPPORT_ASSIGNMENT',
                    customer=ticket.customer,
                    target_id=ticket.ticket_number,
                    description=f"Released ticket #{ticket.ticket_number}",
                    request=request
                )
                messages.success(request, f"Ticket #{ticket.ticket_number} released back to queue ✅")
            else:
                messages.error(request, "You cannot release a ticket that is not assigned to you ❌")
        else:
            messages.error(request, "Invalid assignment action ❌")

    return redirect('staff_support_detail', ticket_id=ticket.id)


@require_POST
@role_required('ADMIN')
def admin_support_assign_view(request, ticket_id):
    """
    Dedicated Admin support ticket assignment endpoint.
    Only ADMIN users can assign tickets to arbitrary staff members or clear assignments.
    Non-admin staff members receive a 403 Forbidden response.
    """
    ticket = get_object_or_404(SupportTicket, id=ticket_id)
    assigned_to_id = request.POST.get('assigned_to', '').strip()

    if assigned_to_id:
        staff_user = get_object_or_404(User, id=assigned_to_id, role__in=['EMPLOYEE', 'ADMIN'], is_active=True)
        ticket.assigned_to = staff_user
        ticket.save(update_fields=['assigned_to', 'updated_at'])
        log_employee_activity(
            employee=request.user,
            action_type='SUPPORT_ASSIGNMENT',
            customer=ticket.customer,
            target_id=ticket.ticket_number,
            description=f"Admin assigned ticket #{ticket.ticket_number} to {staff_user.username}",
            request=request
        )
        send_in_app_notification(
            recipient=staff_user,
            notification_type='SUPPORT',
            title=f"Ticket Assigned: {ticket.ticket_number}",
            message=f"You have been assigned support ticket {ticket.ticket_number} ({ticket.subject}).",
            target_url=f"/bank/staff/support/{ticket.id}/",
            dedup_key=f"support_assign_{ticket.id}_{staff_user.id}"
        )
        messages.success(request, f"Ticket #{ticket.ticket_number} assigned to {staff_user.username} ✅")
    else:
        ticket.assigned_to = None
        ticket.save(update_fields=['assigned_to', 'updated_at'])
        log_employee_activity(
            employee=request.user,
            action_type='SUPPORT_ASSIGNMENT',
            customer=ticket.customer,
            target_id=ticket.ticket_number,
            description=f"Admin cleared assignment for ticket #{ticket.ticket_number}",
            request=request
        )
        messages.success(request, f"Ticket #{ticket.ticket_number} assignment cleared ✅")

    return redirect('staff_support_detail', ticket_id=ticket.id)


# =========================================================================
# FINANCIAL PRODUCT OPERATIONS (TASK 3.3)
# =========================================================================

# -------------------------------------------------------------------------
# 1. EMPLOYEE LOAN VERIFICATION WORKSPACE
# -------------------------------------------------------------------------
@role_required('EMPLOYEE', 'ADMIN')
def staff_loan_queue_view(request):
    """
    Employee loan verification queue.
    Search by customer, application ID, account, and filter by status.
    """
    status_tab = request.GET.get('status', 'PENDING').strip().upper()
    query = request.GET.get('q', '').strip()

    base_qs = Loan.objects.select_related(
        'account', 'account__user', 'reviewed_by', 'admin_decided_by'
    ).order_by('-created_at')

    # Status counts
    pending_count = base_qs.filter(status='PENDING').count()
    under_review_count = base_qs.filter(status='UNDER_REVIEW').count()
    approved_count = base_qs.filter(status='APPROVED').count()
    rejected_count = base_qs.filter(status='REJECTED').count()
    closed_count = base_qs.filter(status='CLOSED').count()
    all_count = base_qs.count()

    qs = base_qs
    if status_tab in ('PENDING', 'UNDER_REVIEW', 'APPROVED', 'REJECTED', 'CLOSED'):
        qs = qs.filter(status=status_tab)
    else:
        status_tab = 'ALL'

    if query:
        filter_q = (
            Q(account__account_number__icontains=query) |
            Q(account__user__username__icontains=query) |
            Q(account__user__first_name__icontains=query) |
            Q(account__user__last_name__icontains=query)
        )
        if query.isdigit():
            filter_q |= Q(id=int(query))
        qs = qs.filter(filter_q)

    paginator = Paginator(qs, 15)
    page_number = request.GET.get('page', 1)
    try:
        page_obj = paginator.page(page_number)
    except (PageNotAnInteger, EmptyPage):
        page_obj = paginator.page(1)

    return render(request, "staff_loan_queue.html", {
        'loans': page_obj,
        'page_obj': page_obj,
        'selected_status': status_tab,
        'search_query': query,
        'pending_count': pending_count,
        'under_review_count': under_review_count,
        'approved_count': approved_count,
        'rejected_count': rejected_count,
        'closed_count': closed_count,
        'all_count': all_count,
        'is_admin': request.user.role == 'ADMIN',
    })


@role_required('EMPLOYEE', 'ADMIN')
def staff_loan_detail_view(request, loan_id):
    """
    Employee loan verification detail workspace.
    Displays customer and account summary, simulation rate and EMI, KYC status,
    existing customer loan history, and verification audit trail.
    """
    loan = get_object_or_404(
        Loan.objects.select_related(
            'account', 'account__user', 'reviewed_by', 'admin_decided_by'
        ),
        id=loan_id
    )
    customer = loan.account.user
    account = loan.account

    # Other loans for this customer
    customer_loans = Loan.objects.filter(
        account__user=customer
    ).exclude(id=loan.id).select_related('account').order_by('-created_at')

    # Verification and decision audit trail
    verification_logs = loan.verification_logs.select_related('performed_by').all().order_by('-created_at')

    # Installment schedule if already approved
    installments = loan.installments.all().order_by('installment_number')
    repayments = loan.repayments.all().order_by('-payment_date')

    return render(request, "staff_loan_detail.html", {
        'loan': loan,
        'customer': customer,
        'account': account,
        'customer_loans': customer_loans,
        'verification_logs': verification_logs,
        'installments': installments,
        'repayments': repayments,
        'is_admin': request.user.role == 'ADMIN',
    })


@require_POST
@role_required('EMPLOYEE', 'ADMIN')
def staff_loan_verify_action_view(request, loan_id):
    """
    Employee loan verification action endpoint.
    Allows marking application UNDER_REVIEW, recording RECOMMEND_APPROVE or RECOMMEND_REJECT
    with mandatory verification notes, and logging verification audit entries.
    Employees CANNOT disburse loan funds; final approval remains Admin-only.
    """
    loan = get_object_or_404(Loan, id=loan_id)
    action_type = request.POST.get('action_type', '').strip().lower()
    notes = request.POST.get('notes', '').strip()

    if action_type == 'under_review':
        if loan.status == 'PENDING':
            loan.status = 'UNDER_REVIEW'
            loan.save(update_fields=['status'])

            LoanVerificationLog.objects.create(
                loan=loan,
                performed_by=request.user,
                action='UNDER_REVIEW',
                notes=notes or 'Application taken up for employee underwriting verification.'
            )
            log_employee_activity(
                employee=request.user,
                action_type='LOAN_VERIFICATION',
                customer=loan.account.user if loan.account else None,
                account_number=loan.account.account_number if loan.account else '',
                target_id=str(loan.id),
                description=f"Marked Loan #{loan.id} Under Review. Notes: {notes or 'Underwriting verification started'}",
                request=request
            )

            if loan.account and loan.account.user:
                send_in_app_notification(
                    recipient=loan.account.user,
                    notification_type='LOAN',
                    title='Loan Application Under Review',
                    message=f"Your loan application #{loan.id} (₹{loan.amount:,.2f}) has been taken up for underwriting review.",
                    target_url=f"/bank/loan/{loan.id}/",
                    dedup_key=f"LOAN-REVIEW-NOTIF-{loan.id}"
                )
            messages.success(request, f"Loan #{loan.id} marked as Under Review 📋")
        else:
            messages.info(request, f"Loan #{loan.id} is already in status '{loan.status}' ℹ️")

    elif action_type == 'recommend_approve':
        if not notes:
            messages.error(request, "Mandatory verification notes are required when recommending approval ❌")
            return redirect('staff_loan_detail', loan_id=loan.id)

        if loan.status in ('PENDING', 'UNDER_REVIEW'):
            loan.status = 'UNDER_REVIEW'
            loan.recommendation = 'RECOMMEND_APPROVE'
            loan.verification_notes = notes
            loan.reviewed_by = request.user
            loan.reviewed_at = timezone.now()
            loan.save(update_fields=['status', 'recommendation', 'verification_notes', 'reviewed_by', 'reviewed_at'])

            LoanVerificationLog.objects.create(
                loan=loan,
                performed_by=request.user,
                action='RECOMMEND_APPROVE',
                notes=notes
            )
            log_employee_activity(
                employee=request.user,
                action_type='LOAN_RECOMMEND_APPROVE',
                customer=loan.account.user if loan.account else None,
                account_number=loan.account.account_number if loan.account else '',
                target_id=str(loan.id),
                description=f"Recommended approval for Loan #{loan.id}. Notes: {notes}",
                changes=notes,
                request=request
            )
            messages.success(request, f"Approval recommendation recorded for Loan #{loan.id} ✅ (Awaiting final administrative decision)")
        else:
            messages.error(request, f"Cannot recommend approval for loan with status '{loan.status}' ❌")

    elif action_type == 'recommend_reject':
        if not notes:
            messages.error(request, "Mandatory verification notes are required when recommending rejection ❌")
            return redirect('staff_loan_detail', loan_id=loan.id)

        if loan.status in ('PENDING', 'UNDER_REVIEW'):
            loan.status = 'UNDER_REVIEW'
            loan.recommendation = 'RECOMMEND_REJECT'
            loan.verification_notes = notes
            loan.reviewed_by = request.user
            loan.reviewed_at = timezone.now()
            loan.save(update_fields=['status', 'recommendation', 'verification_notes', 'reviewed_by', 'reviewed_at'])

            LoanVerificationLog.objects.create(
                loan=loan,
                performed_by=request.user,
                action='RECOMMEND_REJECT',
                notes=notes
            )
            log_employee_activity(
                employee=request.user,
                action_type='LOAN_RECOMMEND_REJECT',
                customer=loan.account.user if loan.account else None,
                account_number=loan.account.account_number if loan.account else '',
                target_id=str(loan.id),
                description=f"Recommended rejection for Loan #{loan.id}. Notes: {notes}",
                changes=notes,
                request=request
            )
            messages.success(request, f"Rejection recommendation recorded for Loan #{loan.id} ℹ️ (Awaiting final administrative decision)")
        else:
            messages.error(request, f"Cannot recommend rejection for loan with status '{loan.status}' ❌")

    elif action_type == 'add_note':
        if not notes:
            messages.error(request, "Verification note cannot be empty ❌")
            return redirect('staff_loan_detail', loan_id=loan.id)

        LoanVerificationLog.objects.create(
            loan=loan,
            performed_by=request.user,
            action='VERIFICATION_NOTE',
            notes=notes
        )
        log_employee_activity(
            employee=request.user,
            action_type='LOAN_VERIFICATION',
            customer=loan.account.user if loan.account else None,
            account_number=loan.account.account_number if loan.account else '',
            target_id=str(loan.id),
            description=f"Added underwriting note to Loan #{loan.id}. Notes: {notes}",
            changes=notes,
            request=request
        )
        messages.success(request, f"Verification note added to Loan #{loan.id} 📝")

    else:
        messages.error(request, "Invalid verification action requested ❌")

    return redirect('staff_loan_detail', loan_id=loan.id)


# -------------------------------------------------------------------------
# 2. FIXED DEPOSIT MONITORING WORKSPACE
# -------------------------------------------------------------------------
@role_required('EMPLOYEE', 'ADMIN')
def staff_fd_list_view(request):
    """
    Employee Fixed Deposit monitoring workspace.
    Read-only tracking of active, matured, and closed deposits.
    Includes upcoming maturities and recently matured unsettled lists.
    """
    status_tab = request.GET.get('status', 'ALL').strip().upper()
    query = request.GET.get('q', '').strip()

    base_qs = FixedDeposit.objects.select_related('account', 'account__user').order_by('-created_at')

    now = timezone.now()
    thirty_days_later = now + timedelta(days=30)

    # Status & Queue counts
    all_count = base_qs.count()
    active_count = base_qs.filter(status='ACTIVE').count()
    matured_count = base_qs.filter(status='MATURED').count()
    closed_count = base_qs.filter(status='CLOSED').count()
    upcoming_count = base_qs.filter(
        status='ACTIVE', maturity_date__gte=now, maturity_date__lte=thirty_days_later
    ).count()
    unsettled_count = base_qs.filter(status='ACTIVE', maturity_date__lte=now).count()

    qs = base_qs
    if status_tab == 'ACTIVE':
        qs = qs.filter(status='ACTIVE')
    elif status_tab == 'MATURED':
        qs = qs.filter(status='MATURED')
    elif status_tab == 'CLOSED':
        qs = qs.filter(status='CLOSED')
    elif status_tab == 'UPCOMING':
        qs = qs.filter(status='ACTIVE', maturity_date__gte=now, maturity_date__lte=thirty_days_later)
    elif status_tab == 'UNSETTLED':
        qs = qs.filter(status='ACTIVE', maturity_date__lte=now)
    else:
        status_tab = 'ALL'

    if query:
        filter_q = (
            Q(account__account_number__icontains=query) |
            Q(account__user__username__icontains=query) |
            Q(account__user__first_name__icontains=query) |
            Q(account__user__last_name__icontains=query)
        )
        if query.isdigit():
            filter_q |= Q(id=int(query))
        qs = qs.filter(filter_q)

    paginator = Paginator(qs, 20)
    page_number = request.GET.get('page', 1)
    try:
        page_obj = paginator.page(page_number)
    except (PageNotAnInteger, EmptyPage):
        page_obj = paginator.page(1)

    return render(request, "staff_fd_list.html", {
        'deposits': page_obj,
        'page_obj': page_obj,
        'selected_status': status_tab,
        'search_query': query,
        'all_count': all_count,
        'active_count': active_count,
        'matured_count': matured_count,
        'closed_count': closed_count,
        'upcoming_count': upcoming_count,
        'unsettled_count': unsettled_count,
    })


@role_required('EMPLOYEE', 'ADMIN')
def staff_fd_detail_view(request, fd_id):
    """
    Employee Fixed Deposit detail & ledger verification view.
    Strictly read-only monitoring: employees cannot alter terms or trigger manual payout.
    """
    fd = get_object_or_404(
        FixedDeposit.objects.select_related('account', 'account__user'),
        id=fd_id
    )
    customer = fd.account.user
    account = fd.account

    # Ledger audit references
    opening_tx = Transaction.objects.filter(
        account=account, transaction_type='FD_DEBIT', amount=fd.amount
    ).order_by('created_at').first()

    settlement_tx = None
    if fd.status in ('MATURED', 'CLOSED'):
        settlement_tx = Transaction.objects.filter(
            account=account,
            transaction_type__in=['FD_MATURITY_CREDIT', 'FD_CLOSURE_CREDIT']
        ).order_by('-created_at').first()

    log_employee_activity(
        employee=request.user,
        action_type='FD_INSPECTION',
        customer=customer,
        account_number=account.account_number,
        target_id=str(fd.id),
        description=f"Inspected Fixed Deposit #{fd.id} terms and ledger records",
        request=request
    )

    return render(request, "staff_fd_detail.html", {
        'fd': fd,
        'customer': customer,
        'account': account,
        'opening_tx': opening_tx,
        'settlement_tx': settlement_tx,
    })


@role_required('EMPLOYEE', 'ADMIN')
def staff_fd_export_csv_view(request):
    """
    Export filtered Fixed Deposit monitoring report to CSV with safe formula escaping.
    """
    status_tab = request.GET.get('status', 'ALL').strip().upper()
    query = request.GET.get('q', '').strip()

    qs = FixedDeposit.objects.select_related('account', 'account__user').order_by('-created_at')

    now = timezone.now()
    thirty_days_later = now + timedelta(days=30)

    if status_tab == 'ACTIVE':
        qs = qs.filter(status='ACTIVE')
    elif status_tab == 'MATURED':
        qs = qs.filter(status='MATURED')
    elif status_tab == 'CLOSED':
        qs = qs.filter(status='CLOSED')
    elif status_tab == 'UPCOMING':
        qs = qs.filter(status='ACTIVE', maturity_date__gte=now, maturity_date__lte=thirty_days_later)
    elif status_tab == 'UNSETTLED':
        qs = qs.filter(status='ACTIVE', maturity_date__lte=now)

    if query:
        filter_q = (
            Q(account__account_number__icontains=query) |
            Q(account__user__username__icontains=query) |
            Q(account__user__first_name__icontains=query) |
            Q(account__user__last_name__icontains=query)
        )
        if query.isdigit():
            filter_q |= Q(id=int(query))
        qs = qs.filter(filter_q)

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    today_str = timezone.localdate().strftime('%Y%m%d')
    response['Content-Disposition'] = f'attachment; filename="bebank_fd_report_{today_str}.csv"'

    writer = csv.writer(response)
    writer.writerow([
        "FD ID",
        "Customer Username",
        "Customer Name",
        "Account Number",
        "Principal (INR)",
        "Interest Rate (%)",
        "Tenure (Months)",
        "Start Date",
        "Maturity Date",
        "Maturity Amount (INR)",
        "Status",
        "Settled At",
        "Closure Type",
    ])

    for fd in qs:
        cust_name = fd.account.user.get_full_name() if fd.account and fd.account.user else ""
        writer.writerow([
            safe_csv_cell(fd.id),
            safe_csv_cell(fd.account.user.username if fd.account and fd.account.user else ""),
            safe_csv_cell(cust_name),
            safe_csv_cell(fd.account.account_number if fd.account else ""),
            safe_csv_cell(f"{fd.amount:.2f}"),
            safe_csv_cell(f"{fd.interest_rate:.2f}"),
            safe_csv_cell(fd.duration_months),
            safe_csv_cell(fd.start_date.strftime('%Y-%m-%d') if fd.start_date else ""),
            safe_csv_cell(fd.maturity_date.strftime('%Y-%m-%d') if fd.maturity_date else ""),
            safe_csv_cell(f"{fd.maturity_amount:.2f}" if fd.maturity_amount else ""),
            safe_csv_cell(fd.status),
            safe_csv_cell(fd.settled_at.strftime('%Y-%m-%d %H:%M') if fd.settled_at else ""),
            safe_csv_cell(fd.closure_type or ""),
        ])

    log_employee_activity(
        employee=request.user,
        action_type='REPORT_EXPORT',
        description=f"Exported Fixed Deposit monitoring report CSV (tab: {status_tab})",
        request=request
    )

    return response


# -------------------------------------------------------------------------
# 3. DEBIT CARD OPERATIONS WORKSPACE
# -------------------------------------------------------------------------
@role_required('EMPLOYEE', 'ADMIN')
def staff_card_list_view(request):
    """
    Employee Debit Card monitoring workspace.
    Search by customer, account, masked card identifier, and filter by status.
    Strictly displays masked card identifiers; never exposes sensitive PAN, CVV, or PIN.
    """
    status_tab = request.GET.get('status', 'ALL').strip().upper()
    query = request.GET.get('q', '').strip()

    base_qs = VirtualDebitCard.objects.select_related('account', 'user').order_by('-created_at')

    all_count = base_qs.count()
    active_count = base_qs.filter(status='ACTIVE').count()
    blocked_count = base_qs.filter(status='BLOCKED').count()
    expired_count = base_qs.filter(status='EXPIRED').count()
    cancelled_count = base_qs.filter(status='CANCELLED').count()

    qs = base_qs
    if status_tab in ('ACTIVE', 'BLOCKED', 'EXPIRED', 'CANCELLED'):
        qs = qs.filter(status=status_tab)
    else:
        status_tab = 'ALL'

    if query:
        filter_q = (
            Q(cardholder_name__icontains=query) |
            Q(account__account_number__icontains=query) |
            Q(user__username__icontains=query) |
            Q(card_number__icontains=query)
        )
        if query.isdigit():
            filter_q |= Q(id=int(query))
        qs = qs.filter(filter_q)

    paginator = Paginator(qs, 20)
    page_number = request.GET.get('page', 1)
    try:
        page_obj = paginator.page(page_number)
    except (PageNotAnInteger, EmptyPage):
        page_obj = paginator.page(1)

    return render(request, "staff_card_list.html", {
        'cards': page_obj,
        'page_obj': page_obj,
        'selected_status': status_tab,
        'search_query': query,
        'all_count': all_count,
        'active_count': active_count,
        'blocked_count': blocked_count,
        'expired_count': expired_count,
        'cancelled_count': cancelled_count,
        'is_admin': request.user.role == 'ADMIN',
    })
