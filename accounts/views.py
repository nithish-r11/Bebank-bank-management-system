import os
import sys
import io
import csv
import sqlite3
import hashlib
import platform
import time
from datetime import datetime, timedelta
import django
from decimal import Decimal, InvalidOperation
from django.conf import settings
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.http import HttpResponseNotAllowed, FileResponse, HttpResponse, Http404, HttpResponseBadRequest
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.password_validation import validate_password
from django.contrib import messages
from django.core.validators import validate_email
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, IntegrityError, transaction, connection
from django.db.models import Sum, Q, Count
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.views.decorators.http import require_POST
from django.utils import timezone

from .models import User, ProfileAuditLog
from .decorators import role_required, _is_locked_or_inactive
from banking.models import (
    BankAccount, Transaction, Loan, LoanRepayment, FixedDeposit, VirtualDebitCard,
    DebitCardAuditLog, SupportTicket, send_in_app_notification, Notification,
    EmployeeActivityLog, log_employee_activity, TellerOperationLog, LoanVerificationLog,
    sanitize_internal_url
)


# =========================
# PUBLIC VIEWS
# =========================
def _get_auth_action_context(user):
    """Compute dynamic action URL and label based on user authentication and role."""
    auth_action_url = reverse('login')
    auth_action_label = "Login"
    show_logout = False

    try:
        is_authenticated = bool(user and user.is_authenticated)
    except DatabaseError:
        is_authenticated = False

    if is_authenticated:
        try:
            if not _is_locked_or_inactive(user):
                role = getattr(user, 'role', None)
                if role == 'CUSTOMER':
                    auth_action_url = reverse('customer_dashboard')
                    auth_action_label = "Go to Customer Dashboard"
                    show_logout = True
                elif role == 'EMPLOYEE':
                    auth_action_url = reverse('employee_dashboard')
                    auth_action_label = "Go to Employee Dashboard"
                    show_logout = True
                elif role == 'ADMIN':
                    auth_action_url = reverse('admin_dashboard')
                    auth_action_label = "Go to Admin Dashboard"
                    show_logout = True
        except DatabaseError:
            auth_action_url = reverse('login')
            auth_action_label = "Login"
            show_logout = False

    return {
        "auth_action_url": auth_action_url,
        "auth_action_label": auth_action_label,
        "show_logout": show_logout,
    }


def terms_view(request):
    return render(request, "terms.html", _get_auth_action_context(request.user))


# =========================
# ✅ SINGLE LOGIN
# =========================
def login_view(request):

    if request.method == 'POST':

        username = request.POST.get("username")
        password = request.POST.get("password")
        role = request.POST.get("role")

        user = authenticate(request, username=username, password=password)

        if user is not None:

            # 🔴 BLOCK CHECK
            if getattr(user, "account_locked", False):
                messages.error(request, "Account is blocked ❌")
                return redirect("login")

            # 🔴 ROLE CHECK
            if user.role != role:
                messages.error(request, "Invalid role selected ❌")
                return redirect("login")

            login(request, user)

            # 🔀 REDIRECT BASED ON ROLE
            if role == "CUSTOMER":
                return redirect(reverse("customer_dashboard"))
            elif role == "EMPLOYEE":
                return redirect(reverse("employee_dashboard"))
            elif role == "ADMIN":
                return redirect(reverse("admin_dashboard"))

        else:
            messages.error(request, "Invalid username or password ❌")

    return render(request, "login.html", _get_auth_action_context(request.user))


# =========================
# CUSTOMER DASHBOARD
# =========================
@role_required('CUSTOMER')
def customer_dashboard(request, account_id=None):

    account_id = account_id or request.GET.get('account_id') or request.POST.get('account_id')
    if account_id:
        account = get_object_or_404(BankAccount, id=account_id, user=request.user)
    else:
        account = BankAccount.objects.filter(user=request.user).last()

    accounts = BankAccount.objects.filter(user=request.user).order_by('-created_at')
    unread_notifications_count = Notification.objects.filter(recipient=request.user, is_read=False).count()

    return render(request, "customer_dashboard.html", {
        "account": account,
        "accounts": accounts,
        "completeness": request.user.get_profile_completeness(),
        "unread_notifications_count": unread_notifications_count,
    })


# =========================
# CUSTOMER PROFILE (READ-ONLY FOR CUSTOMERS)
# =========================
@role_required('CUSTOMER')
def customer_profile_view(request):
    """
    Customer profile view.
    Strictly read-only for customers; profile updates must be conducted by authorized bank staff.
    """
    if request.method == 'POST':
        return HttpResponseNotAllowed(['GET'], "Customer profile editing is disabled. Updates are managed by bank staff.")

    user = request.user
    accounts = BankAccount.objects.filter(user=user).order_by('-created_at')
    completeness = user.get_profile_completeness()
    return render(request, "customer_profile.html", {
        'user': user,
        'accounts': accounts,
        'completeness': completeness,
    })


# =========================
# STAFF-CONTROLLED CUSTOMER PROFILE UPDATE
# =========================
@role_required('EMPLOYEE', 'ADMIN')
def staff_customer_profile_edit_view(request, user_id):
    """
    Staff-controlled customer profile update.
    Permits Employee and Admin roles to update customer contact/profile fields:
    first_name, last_name, email, phone, address, pincode.
    Strictly prohibits modification of username, role, flags, security/balance attributes.
    Logs each change to ProfileAuditLog and records updater and timestamp.
    """
    target_user = get_object_or_404(User, id=user_id, role='CUSTOMER')
    linked_accounts = BankAccount.objects.filter(user=target_user).order_by('-created_at')

    if request.method == 'POST':
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        email = request.POST.get('email', '').strip()
        phone = request.POST.get('phone', '').strip()
        address = request.POST.get('address', '').strip()
        pincode = request.POST.get('pincode', '').strip()

        # Input validation
        if email and '@' not in email:
            messages.error(request, "Please enter a valid email address ❌")
            return redirect('staff_customer_profile_edit', user_id=target_user.id)

        if phone and (len(phone) < 10 or len(phone) > 15):
            messages.error(request, "Phone number must be between 10 and 15 digits ❌")
            return redirect('staff_customer_profile_edit', user_id=target_user.id)

        if pincode and (len(pincode) < 4 or len(pincode) > 10):
            messages.error(request, "Please enter a valid postal code ❌")
            return redirect('staff_customer_profile_edit', user_id=target_user.id)

        # Audit differences
        changes = []
        if target_user.first_name != first_name[:50]:
            changes.append(f"First Name: '{target_user.first_name}' -> '{first_name[:50]}'")
        if target_user.last_name != last_name[:50]:
            changes.append(f"Last Name: '{target_user.last_name}' -> '{last_name[:50]}'")
        if email and target_user.email != email[:100]:
            changes.append(f"Email: '{target_user.email}' -> '{email[:100]}'")
        if target_user.phone != phone[:15]:
            changes.append(f"Phone: '{target_user.phone}' -> '{phone[:15]}'")
        if target_user.address != address:
            changes.append("Address updated")
        if target_user.pincode != pincode[:10]:
            changes.append(f"Pincode: '{target_user.pincode}' -> '{pincode[:10]}'")

        # Strict Mass Assignment Protection: Whitelist permitted fields only
        target_user.first_name = first_name[:50]
        target_user.last_name = last_name[:50]
        if email:
            target_user.email = email[:100]
        target_user.phone = phone[:15]
        target_user.address = address
        target_user.pincode = pincode[:10]
        target_user.profile_updated_by = request.user
        target_user.profile_updated_at = timezone.now()
        target_user.save(update_fields=[
            'first_name', 'last_name', 'email', 'phone', 'address', 'pincode',
            'profile_updated_by', 'profile_updated_at'
        ])

        # Keep linked BankAccounts in sync with updated profile
        BankAccount.objects.filter(user=target_user).update(
            email=target_user.email,
            phone=target_user.phone,
            address=target_user.address,
            pincode=target_user.pincode
        )

        # Record ProfileAuditLog
        change_summary = "; ".join(changes) if changes else "Profile details re-confirmed by staff"
        ProfileAuditLog.objects.create(
            user=target_user,
            updated_by=request.user,
            changes=f"{change_summary} (by {request.user.username} - {request.user.role})",
            ip_address=request.META.get('REMOTE_ADDR')
        )
        log_employee_activity(
            employee=request.user,
            action_type='PROFILE_UPDATE',
            description=f"Updated profile for customer {target_user.username}",
            customer=target_user,
            target_id=str(target_user.id),
            changes=change_summary,
            request=request
        )

        send_in_app_notification(
            recipient=target_user,
            notification_type='ACCOUNT',
            title='Customer Profile Updated',
            message=f"Your profile and contact details have been updated by bank staff ({request.user.username}).",
            target_url='/accounts/customer/profile/'
        )

        messages.success(request, f"Customer profile for '{target_user.username}' updated successfully ✅")
        return redirect('staff_customer_profile_edit', user_id=target_user.id)

    audit_logs = ProfileAuditLog.objects.filter(user=target_user).order_by('-updated_at')[:15]
    completeness = target_user.get_profile_completeness()

    return render(request, "staff_customer_profile_edit.html", {
        'target_user': target_user,
        'linked_accounts': linked_accounts,
        'audit_logs': audit_logs,
        'completeness': completeness,
    })



# =========================
# SIMULATED KYC WORKFLOW
# =========================
@role_required('CUSTOMER')
def customer_kyc_view(request):
    user = request.user

    if request.method == 'POST':
        # Prevent unauthorized modification of approved KYC records
        if user.kyc_status == 'APPROVED':
            messages.error(request, "Your KYC is already approved and verified. Modifications are not permitted ❌")
            return redirect('customer_kyc')

        # Prevent concurrent resubmission when under review
        if user.kyc_status in ('PENDING', 'UNDER_REVIEW'):
            messages.error(request, "Your KYC submission is currently under review. Please wait for verification ❌")
            return redirect('customer_kyc')

        doc_type = request.POST.get('simulated_id_type', '').strip()
        doc_number = request.POST.get('simulated_id_number', '').strip()

        valid_types = dict(User.SIMULATED_DOC_CHOICES)
        if doc_type not in valid_types:
            messages.error(request, "Please select a valid simulated document type ❌")
            return redirect('customer_kyc')

        if not doc_number or len(doc_number) < 3 or len(doc_number) > 50:
            messages.error(request, "Please enter a valid simulated document reference number (3-50 characters) ❌")
            return redirect('customer_kyc')

        # Update KYC status to PENDING and clear prior rejection reason
        user.simulated_id_type = doc_type
        user.simulated_id_number = doc_number
        user.kyc_status = 'PENDING'
        user.kyc_rejection_reason = ''
        user.kyc_submitted_at = timezone.now()
        user.kyc_verified = False
        user.save(update_fields=[
            'simulated_id_type',
            'simulated_id_number',
            'kyc_status',
            'kyc_rejection_reason',
            'kyc_submitted_at',
            'kyc_verified',
        ])

        send_in_app_notification(
            recipient=user,
            notification_type='KYC',
            title='KYC Documents Submitted',
            message='Your simulated identity verification documents have been received and are pending review.',
            target_url='/accounts/kyc/'
        )

        messages.success(request, "Simulated KYC submitted successfully ✅ (Status: Pending Verification)")
        return redirect('customer_kyc')

    return render(request, "customer_kyc.html", {
        'user': user,
        'doc_choices': User.SIMULATED_DOC_CHOICES,
    })


# =========================
# DATA MASKING HELPERS (SENSITIVE DATA PROTECTION)
# =========================
def mask_aadhaar(val):
    """Mask simulated Aadhaar number for employee views (show last 4 digits only)."""
    if not val:
        return '-'
    s = str(val).replace(' ', '').replace('-', '').strip()
    if len(s) >= 4:
        return f"•••• •••• {s[-4:]}"
    return '•••• •••• ••••'


def mask_pan(val):
    """Mask simulated PAN number for employee views (show prefix & last 2 digits)."""
    if not val:
        return '-'
    s = str(val).strip().upper()
    if len(s) == 10:
        return f"{s[:2]}••••••{s[-2:]}"
    if len(s) >= 4:
        return f"••••••{s[-4:]}"
    return '••••••••'


def mask_simulated_id(val):
    """Mask simulated document reference for employee queue & review."""
    if not val:
        return '-'
    s = str(val).strip()
    if len(s) > 4:
        return f"{'•' * (len(s) - 4)}{s[-4:]}"
    return s


# =========================
# STAFF KYC REVIEW WORKFLOW
# =========================
@require_POST
@role_required('EMPLOYEE', 'ADMIN')
def staff_kyc_review_view(request, user_id):
    """
    Staff review action on customer KYC submissions.
    Supports approve, reject (with mandatory reason), and under_review.
    Strictly forbids self-review and prevents silent overwrite of approved records.
    """
    target_user = get_object_or_404(User, id=user_id, role='CUSTOMER')
    next_url = request.POST.get('next') or request.META.get('HTTP_REFERER') or reverse('staff_kyc_queue')

    # Security: Prevent staff from reviewing their own KYC
    if request.user.id == target_user.id:
        messages.error(request, "Self-review of KYC is strictly prohibited ❌")
        return redirect(next_url)

    action = request.POST.get('action', '').strip().lower()

    if action == 'approve':
        if target_user.kyc_status == 'APPROVED':
            messages.info(request, f"Simulated KYC for '{target_user.username}' is already approved ℹ️")
            return redirect(next_url)

        target_user.kyc_status = 'APPROVED'
        target_user.kyc_verified = True
        target_user.kyc_rejection_reason = ''
        target_user.kyc_reviewed_at = timezone.now()
        target_user.kyc_reviewed_by = request.user
        target_user.save(update_fields=[
            'kyc_status', 'kyc_verified', 'kyc_rejection_reason',
            'kyc_reviewed_at', 'kyc_reviewed_by'
        ])
        send_in_app_notification(
            recipient=target_user,
            notification_type='KYC',
            title='KYC Verification Approved',
            message='Your simulated identity verification has been approved. Your profile is verified.',
            target_url='/accounts/kyc/'
        )
        log_employee_activity(
            employee=request.user,
            action_type='KYC_APPROVED',
            description=f"Approved KYC for customer {target_user.username}",
            customer=target_user,
            target_id=str(target_user.id),
            request=request
        )
        messages.success(request, f"Simulated KYC for '{target_user.username}' has been approved ✅")

    elif action == 'reject':
        reason = request.POST.get('rejection_reason', '').strip()
        if not reason:
            messages.error(request, "Please provide a specific rejection reason ❌")
            return redirect(next_url)

        # Protection: Prevent silent revocation of approved KYC
        if target_user.kyc_status == 'APPROVED':
            confirm = request.POST.get('confirm_reversal', '').strip().lower()
            if confirm not in ('true', 'yes', '1'):
                messages.error(request, "Cannot silently revoke approved KYC. Explicit confirmation required ❌")
                return redirect(next_url)

        target_user.kyc_status = 'REJECTED'
        target_user.kyc_verified = False
        target_user.kyc_rejection_reason = reason
        target_user.kyc_reviewed_at = timezone.now()
        target_user.kyc_reviewed_by = request.user
        target_user.save(update_fields=[
            'kyc_status', 'kyc_verified', 'kyc_rejection_reason',
            'kyc_reviewed_at', 'kyc_reviewed_by'
        ])
        send_in_app_notification(
            recipient=target_user,
            notification_type='KYC',
            title='KYC Verification Rejected',
            message=f'Your simulated identity verification was not approved. Reason: {reason}',
            target_url='/accounts/kyc/'
        )
        log_employee_activity(
            employee=request.user,
            action_type='KYC_REJECTED',
            description=f"Rejected KYC for customer {target_user.username}. Reason: {reason}",
            customer=target_user,
            target_id=str(target_user.id),
            changes=reason,
            request=request
        )
        messages.warning(request, f"Simulated KYC for '{target_user.username}' has been rejected ❌")

    elif action == 'under_review':
        if target_user.kyc_status == 'APPROVED':
            messages.error(request, "Approved KYC cannot be moved back to Under Review ❌")
            return redirect(next_url)

        target_user.kyc_status = 'UNDER_REVIEW'
        target_user.kyc_reviewed_by = request.user
        target_user.save(update_fields=['kyc_status', 'kyc_reviewed_by'])
        send_in_app_notification(
            recipient=target_user,
            notification_type='KYC',
            title='KYC Verification Under Review',
            message='Your simulated identity documents are currently under review by our operations team.',
            target_url='/accounts/kyc/'
        )
        log_employee_activity(
            employee=request.user,
            action_type='KYC_REVIEW',
            description=f"Marked KYC as Under Review for customer {target_user.username}",
            customer=target_user,
            target_id=str(target_user.id),
            request=request
        )
        messages.info(request, f"Simulated KYC for '{target_user.username}' is now under review ℹ️")

    else:
        messages.error(request, "Invalid review action specified ❌")

    return redirect(next_url)


# =========================
# STAFF KYC QUEUE WORKSPACE
# =========================
@role_required('EMPLOYEE', 'ADMIN')
def staff_kyc_queue_view(request):
    """
    Employee KYC queue workspace.
    Filters by PENDING, UNDER_REVIEW, APPROVED, REJECTED, or ALL.
    Provides counts, search by name/username/email/phone/simulated ID, and pagination.
    """
    status_tab = request.GET.get('status', 'PENDING').strip().upper()
    q = request.GET.get('q', '').strip()

    base_qs = User.objects.filter(role='CUSTOMER')

    # Tab counts
    pending_count = base_qs.filter(kyc_status='PENDING').count()
    under_review_count = base_qs.filter(kyc_status='UNDER_REVIEW').count()
    approved_count = base_qs.filter(kyc_status='APPROVED').count()
    rejected_count = base_qs.filter(kyc_status='REJECTED').count()
    all_count = base_qs.exclude(kyc_status='NOT_SUBMITTED').count()

    qs = base_qs
    if status_tab in ('PENDING', 'UNDER_REVIEW', 'APPROVED', 'REJECTED'):
        qs = qs.filter(kyc_status=status_tab)
    elif status_tab == 'ALL':
        qs = qs.exclude(kyc_status='NOT_SUBMITTED')
    else:
        status_tab = 'PENDING'
        qs = qs.filter(kyc_status='PENDING')

    if q:
        qs = qs.filter(
            Q(username__icontains=q) |
            Q(first_name__icontains=q) |
            Q(last_name__icontains=q) |
            Q(email__icontains=q) |
            Q(phone__icontains=q) |
            Q(simulated_id_number__icontains=q)
        ).distinct()

    qs = qs.order_by('-kyc_submitted_at', '-date_joined')

    paginator = Paginator(qs, 15)
    page_number = request.GET.get('page', 1)
    try:
        page_obj = paginator.page(page_number)
    except (PageNotAnInteger, EmptyPage):
        page_obj = paginator.page(1)

    for u in page_obj:
        u.masked_simulated_id = mask_simulated_id(u.simulated_id_number)

    return render(request, "staff_kyc_queue.html", {
        'page_obj': page_obj,
        'status_tab': status_tab,
        'q': q,
        'pending_count': pending_count,
        'under_review_count': under_review_count,
        'approved_count': approved_count,
        'rejected_count': rejected_count,
        'all_count': all_count,
    })


# =========================
# STAFF CUSTOMER DIRECTORY & SEARCH
# =========================
@role_required('EMPLOYEE', 'ADMIN')
def staff_customer_list_view(request):
    """
    Customer search & directory workspace for bank employees.
    Supports search across username, first/last name, email, phone, and account number.
    """
    q = request.GET.get('q', '').strip()
    kyc_filter = request.GET.get('kyc', '').strip().upper()
    status_filter = request.GET.get('status', '').strip().lower()

    customers = User.objects.filter(role='CUSTOMER').prefetch_related('bankaccount_set').order_by('-date_joined')

    if q:
        customers = customers.filter(
            Q(username__icontains=q) |
            Q(first_name__icontains=q) |
            Q(last_name__icontains=q) |
            Q(email__icontains=q) |
            Q(phone__icontains=q) |
            Q(bankaccount__account_number__icontains=q)
        ).distinct()

    if kyc_filter in dict(User.KYC_STATUS_CHOICES):
        customers = customers.filter(kyc_status=kyc_filter)

    if status_filter == 'locked':
        customers = customers.filter(account_locked=True)
    elif status_filter == 'active':
        customers = customers.filter(account_locked=False, is_active=True)

    total_count = customers.count()
    paginator = Paginator(customers, 15)
    page_number = request.GET.get('page', 1)
    try:
        page_obj = paginator.page(page_number)
    except (PageNotAnInteger, EmptyPage):
        page_obj = paginator.page(1)

    # Attach computed aggregates for template display
    for cust in page_obj:
        accounts = list(cust.bankaccount_set.all())
        cust.accounts_list = accounts
        cust.total_balance = sum((acc.balance for acc in accounts), Decimal('0.00'))
        cust.primary_account = accounts[0] if accounts else None

    return render(request, "staff_customer_list.html", {
        'page_obj': page_obj,
        'q': q,
        'kyc_filter': kyc_filter,
        'status_filter': status_filter,
        'total_count': total_count,
        'kyc_choices': User.KYC_STATUS_CHOICES,
    })


# =========================
# STAFF CUSTOMER 360° PROFILE VIEW
# =========================
@role_required('EMPLOYEE', 'ADMIN')
def staff_customer_detail_view(request, user_id):
    """
    360-degree customer profile view for bank staff.
    Displays linked accounts, balances, transactions, KYC, loans, FDs, cards, tickets, audit logs.
    Strictly forbids modifications to role, password, superuser flags, or balances.
    Sensitive identifiers (Aadhaar, PAN, simulated ID) are masked.
    """
    target_user = get_object_or_404(User, id=user_id, role='CUSTOMER')

    accounts = list(BankAccount.objects.filter(user=target_user).order_by('-created_at'))
    total_balance = sum((acc.balance for acc in accounts), Decimal('0.00'))

    # Recent transactions across all customer accounts
    transactions = Transaction.objects.filter(
        account__user=target_user
    ).select_related('account').order_by('-created_at')[:15]

    # Fixed Deposits
    fixed_deposits = FixedDeposit.objects.filter(
        account__user=target_user
    ).select_related('account').order_by('-created_at')

    # Loans
    loans = Loan.objects.filter(
        account__user=target_user
    ).select_related('account').order_by('-created_at')

    # Virtual Debit Cards
    cards = VirtualDebitCard.objects.filter(
        account__user=target_user
    ).select_related('account').order_by('-created_at')

    # Support Tickets
    tickets = SupportTicket.objects.filter(
        customer=target_user
    ).order_by('-created_at')

    # Profile Audit Logs
    audit_logs = ProfileAuditLog.objects.filter(
        user=target_user
    ).select_related('updated_by').order_by('-updated_at')[:10]

    masked_aadhaar = mask_aadhaar(target_user.aadhaar or (accounts[0].aadhaar if accounts else ''))
    masked_pan = mask_pan(target_user.pan or (accounts[0].pan if accounts else ''))
    masked_simulated_id = mask_simulated_id(target_user.simulated_id_number)

    return render(request, "staff_customer_detail.html", {
        'target_user': target_user,
        'accounts': accounts,
        'total_balance': total_balance,
        'transactions': transactions,
        'fixed_deposits': fixed_deposits,
        'loans': loans,
        'cards': cards,
        'tickets': tickets,
        'audit_logs': audit_logs,
        'masked_aadhaar': masked_aadhaar,
        'masked_pan': masked_pan,
        'masked_simulated_id': masked_simulated_id,
        'completeness': target_user.get_profile_completeness(),
    })


# =========================
# TELLER OPERATIONS (DELEGATED TO BANKING MODULE FOR TASK 3.2)
# =========================
from banking.views import (
    staff_operations_view,
    staff_withdraw_view,
    staff_deposit_view,
    staff_teller_history_view,
    staff_teller_receipt_view,
    staff_teller_export_csv_view,
    create_account_view,
    staff_loan_queue_view,
    staff_loan_detail_view,
    staff_loan_verify_action_view,
    staff_fd_list_view,
    staff_fd_detail_view,
    staff_fd_export_csv_view,
    staff_card_list_view,
    staff_card_detail_view,
    admin_card_action_view,
    approve_loan,
    reject_loan,
    staff_support_queue_view,
    staff_support_detail_view,
    staff_support_reply_view,
    staff_support_assign_view,
    admin_support_assign_view,
    analytics_view,
    staff_reports_export_csv_view,
    staff_activity_log_view,
    staff_activity_export_csv_view,
)


# =========================
# STAFF PERSONAL PROFILE VIEW
# =========================
@role_required('EMPLOYEE', 'ADMIN')
def staff_profile_view(request):
    """
    Staff personal profile and audit activity view.
    """
    staff_user = request.user
    kyc_reviews_count = User.objects.filter(kyc_reviewed_by=staff_user).count()
    profile_edits_count = ProfileAuditLog.objects.filter(updated_by=staff_user).count()
    tickets_assigned_count = SupportTicket.objects.filter(assigned_to=staff_user).count()

    recent_kycs = User.objects.filter(kyc_reviewed_by=staff_user).order_by('-kyc_reviewed_at')[:5]
    recent_edits = ProfileAuditLog.objects.filter(updated_by=staff_user).order_by('-updated_at')[:5]

    return render(request, "staff_profile.html", {
        'staff_user': staff_user,
        'kyc_reviews_count': kyc_reviews_count,
        'profile_edits_count': profile_edits_count,
        'tickets_assigned_count': tickets_assigned_count,
        'recent_kycs': recent_kycs,
        'recent_edits': recent_edits,
    })


# =========================
# EMPLOYEE DASHBOARD
# =========================
@role_required('EMPLOYEE', 'ADMIN')
def employee_dashboard(request):
    """
    Comprehensive Employee Dashboard with live metrics, quick actions,
    search, and recent operational activity.
    """
    total_customers = User.objects.filter(role='CUSTOMER').count()
    total_active_accounts = BankAccount.objects.filter(user__account_locked=False).count()
    total_accounts = BankAccount.objects.count()
    total_balance = BankAccount.objects.aggregate(Sum('balance'))['balance__sum'] or Decimal('0.00')
    total_loans = Loan.objects.count()
    pending_kyc = User.objects.filter(role='CUSTOMER', kyc_status='PENDING').count()
    pending_tickets = SupportTicket.objects.filter(status__in=['OPEN', 'IN_PROGRESS']).count()

    today = timezone.localdate()
    today_deposits = Transaction.objects.filter(
        transaction_type='DEPOSIT',
        created_at__date=today
    ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

    today_withdrawals = Transaction.objects.filter(
        transaction_type='WITHDRAW',
        created_at__date=today
    ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

    account = None
    customer_matches = []
    search_query = request.POST.get("account_number") or request.GET.get("q") or ""
    search_query = search_query.strip()

    if search_query:
        # Check direct account number match
        account = BankAccount.objects.filter(account_number=search_query).first()
        if not account:
            customer_matches = list(User.objects.filter(role='CUSTOMER').filter(
                Q(username__icontains=search_query) |
                Q(first_name__icontains=search_query) |
                Q(last_name__icontains=search_query) |
                Q(email__icontains=search_query) |
                Q(phone__icontains=search_query) |
                Q(bankaccount__account_number__icontains=search_query)
            ).distinct()[:5])
            if customer_matches and len(customer_matches) == 1:
                account = BankAccount.objects.filter(user=customer_matches[0]).first()

        if not account and not customer_matches and request.method == "POST":
            messages.error(request, f"No customer or account found for '{search_query}' ❌")

    # Recent employee activity
    recent_transactions = Transaction.objects.select_related('account', 'account__user').order_by('-created_at')[:8]
    recent_kyc_reviews = User.objects.filter(
        kyc_reviewed_by__isnull=False
    ).select_related('kyc_reviewed_by').order_by('-kyc_reviewed_at')[:5]

    # Personalized officer workload & audit tracking
    my_pending_tickets = SupportTicket.objects.filter(
        assigned_to=request.user,
        status__in=['OPEN', 'IN_PROGRESS', 'WAITING_FOR_CUSTOMER']
    ).count()
    my_kyc_reviews = User.objects.filter(kyc_reviewed_by=request.user).count()
    my_teller_operations_today = TellerOperationLog.objects.filter(
        employee=request.user,
        created_at__date=today
    ).count()
    my_teller_volume_today = TellerOperationLog.objects.filter(
        employee=request.user,
        created_at__date=today,
        status='SUCCESS'
    ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
    my_loan_verifications = LoanVerificationLog.objects.filter(
        performed_by=request.user
    ).count()
    my_recent_actions = EmployeeActivityLog.objects.filter(
        employee=request.user
    ).select_related('customer').order_by('-created_at')[:8]

    return render(request, "employee_dashboard.html", {
        # Legacy keys for backward compatibility
        "accounts": total_accounts,
        "balance": total_balance,
        "loans": total_loans,
        "account": account,
        # Enhanced dashboard metrics
        "total_customers": total_customers,
        "total_active_accounts": total_active_accounts,
        "pending_kyc": pending_kyc,
        "today_deposits": today_deposits,
        "today_withdrawals": today_withdrawals,
        "pending_tickets": pending_tickets,
        "recent_transactions": recent_transactions,
        "recent_kyc_reviews": recent_kyc_reviews,
        "customer_matches": customer_matches,
        "search_query": search_query,
        # Personalized workload & activity
        "my_pending_tickets": my_pending_tickets,
        "my_kyc_reviews": my_kyc_reviews,
        "my_teller_operations_today": my_teller_operations_today,
        "my_teller_volume_today": my_teller_volume_today,
        "my_loan_verifications": my_loan_verifications,
        "my_recent_actions": my_recent_actions,
    })


# =========================================================================
# ADMIN CONTROL CENTER & EMPLOYEE GOVERNANCE (TASK 4.1)
# =========================================================================

@role_required('ADMIN')
def admin_dashboard(request):
    """
    Live database-driven Admin Control Center.
    Aggregates comprehensive system telemetry across customers, staff, accounts,
    ledger balances, daily operational volumes, queues, and security audit trail.
    """
    total_customers = User.objects.filter(role='CUSTOMER').count()
    total_employees = User.objects.filter(role='EMPLOYEE').count()
    total_admins = User.objects.filter(role='ADMIN').count()

    active_users = User.objects.filter(is_active=True, account_locked=False).count()
    locked_users = User.objects.filter(Q(account_locked=True) | Q(is_active=False)).count()

    total_active_accounts = BankAccount.objects.filter(user__is_active=True, user__account_locked=False).count()
    total_accounts = BankAccount.objects.count()
    total_balance = BankAccount.objects.aggregate(Sum('balance'))['balance__sum'] or Decimal('0.00')

    # Today's transactional volumes
    today = timezone.localdate() if hasattr(timezone, 'localdate') else timezone.now().date()

    today_deposits = Transaction.objects.filter(transaction_type='DEPOSIT', created_at__date=today)
    today_deposits_count = today_deposits.count()
    today_deposits_volume = today_deposits.aggregate(Sum('amount'))['amount__sum'] or Decimal('0.00')

    today_withdrawals = Transaction.objects.filter(transaction_type='WITHDRAW', created_at__date=today)
    today_withdrawals_count = today_withdrawals.count()
    today_withdrawals_volume = today_withdrawals.aggregate(Sum('amount'))['amount__sum'] or Decimal('0.00')

    today_transfers = Transaction.objects.filter(transaction_type='TRANSFER_DEBIT', created_at__date=today)
    today_transfers_volume = today_transfers.aggregate(Sum('amount'))['amount__sum'] or Decimal('0.00')

    # Financial product queues
    pending_kyc_count = User.objects.filter(kyc_status__in=['PENDING', 'UNDER_REVIEW']).count()
    pending_loans_count = Loan.objects.filter(status__in=['PENDING', 'UNDER_REVIEW']).count()

    active_fds_count = FixedDeposit.objects.filter(status='ACTIVE').count()
    maturing_fds_count = FixedDeposit.objects.filter(
        status='ACTIVE',
        maturity_date__lte=timezone.now() + timezone.timedelta(days=30)
    ).count()

    active_cards_count = VirtualDebitCard.objects.filter(status='ACTIVE').count()
    blocked_cards_count = VirtualDebitCard.objects.filter(status='BLOCKED').count()
    expired_cards_count = VirtualDebitCard.objects.filter(status='EXPIRED').count()

    open_tickets_count = SupportTicket.objects.filter(
        status__in=['OPEN', 'IN_PROGRESS', 'WAITING_FOR_CUSTOMER']
    ).count()

    recent_activity = EmployeeActivityLog.objects.select_related('employee', 'customer').all().order_by('-created_at')[:10]

    # Latest backup indicator
    latest_backup = None
    backup_dir = os.path.join(settings.BASE_DIR, 'backups')
    if os.path.exists(backup_dir):
        files = [f for f in os.listdir(backup_dir) if f.endswith('.sqlite3')]
        if files:
            files.sort(key=lambda x: os.path.getmtime(os.path.join(backup_dir, x)), reverse=True)
            latest_file = files[0]
            mtime = os.path.getmtime(os.path.join(backup_dir, latest_file))
            latest_backup = {
                'filename': latest_file,
                'created_at': timezone.datetime.fromtimestamp(mtime, tz=timezone.get_current_timezone()).strftime("%Y-%m-%d %H:%M")
            }

    return render(request, 'admin_dashboard.html', {
        'total_customers': total_customers,
        'total_employees': total_employees,
        'total_admins': total_admins,
        'active_users': active_users,
        'locked_users': locked_users,
        'total_active_accounts': total_active_accounts,
        'total_accounts': total_accounts,
        'total_balance': total_balance,
        'today_deposits_count': today_deposits_count,
        'today_deposits_volume': today_deposits_volume,
        'today_withdrawals_count': today_withdrawals_count,
        'today_withdrawals_volume': today_withdrawals_volume,
        'today_transfers_volume': today_transfers_volume,
        'pending_kyc_count': pending_kyc_count,
        'pending_loans_count': pending_loans_count,
        'active_fds_count': active_fds_count,
        'maturing_fds_count': maturing_fds_count,
        'active_cards_count': active_cards_count,
        'blocked_cards_count': blocked_cards_count,
        'expired_cards_count': expired_cards_count,
        'open_tickets_count': open_tickets_count,
        'recent_activity': recent_activity,
        'latest_backup': latest_backup,
        # Legacy context keys compatibility
        'customers': total_customers,
        'employees': total_employees,
        'balance': total_balance,
        'transactions': Transaction.objects.count(),
        'loans': Loan.objects.all(),
    })


@role_required('ADMIN')
def admin_employee_list_view(request):
    """
    Searchable, filterable, paginated staff directory for administrators.
    """
    query = request.GET.get('q', '').strip()
    status_filter = request.GET.get('status', '').strip().lower()
    lock_filter = request.GET.get('lock', '').strip().lower()

    employees = User.objects.filter(role='EMPLOYEE').order_by('-date_joined')

    if query:
        employees = employees.filter(
            Q(username__icontains=query) |
            Q(first_name__icontains=query) |
            Q(last_name__icontains=query) |
            Q(email__icontains=query) |
            Q(phone__icontains=query)
        )

    if status_filter == 'active':
        employees = employees.filter(is_active=True)
    elif status_filter == 'inactive':
        employees = employees.filter(is_active=False)

    if lock_filter == 'locked':
        employees = employees.filter(account_locked=True)
    elif lock_filter == 'unlocked':
        employees = employees.filter(account_locked=False)

    paginator = Paginator(employees, 10)
    page_number = request.GET.get('page')
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    return render(request, 'admin_employee_list.html', {
        'page_obj': page_obj,
        'query': query,
        'selected_status': status_filter,
        'selected_lock': lock_filter,
    })


@role_required('ADMIN')
def admin_employee_create_view(request):
    """
    Secure workflow for provisioning new EMPLOYEE accounts.
    Explicitly enforces role='EMPLOYEE', is_superuser=False, is_staff=False.
    Enforces active Django password validators, uniqueness checks, and audit logging.
    """
    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        email = request.POST.get('email', '').strip()
        password = request.POST.get('password', '')
        confirm_password = request.POST.get('confirm_password', '')
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        phone = request.POST.get('phone', '').strip()
        address = request.POST.get('address', '').strip()
        pincode = request.POST.get('pincode', '').strip()

        form_data = {
            'username': username,
            'email': email,
            'first_name': first_name,
            'last_name': last_name,
            'phone': phone,
            'address': address,
            'pincode': pincode
        }

        # Validations
        if not username or not email or not password or not confirm_password:
            messages.error(request, "Username, email, and passwords are required ❌")
            return render(request, 'admin_employee_create.html', {'form_data': form_data})

        if password != confirm_password:
            messages.error(request, "Passwords do not match ❌")
            return render(request, 'admin_employee_create.html', {'form_data': form_data})

        if User.objects.filter(username__iexact=username).exists():
            messages.error(request, f"Username '{username}' is already taken ❌")
            return render(request, 'admin_employee_create.html', {'form_data': form_data})

        try:
            validate_email(email)
        except ValidationError:
            messages.error(request, "Please enter a valid email address ❌")
            return render(request, 'admin_employee_create.html', {'form_data': form_data})

        # Validate password using active Django password validators
        temp_user = User(username=username, email=email)
        try:
            validate_password(password, user=temp_user)
        except ValidationError as e:
            for err in e.messages:
                messages.error(request, f"Password error: {err}")
            return render(request, 'admin_employee_create.html', {'form_data': form_data})

        # Create user with strict role boundaries
        user = User(
            username=username,
            email=email,
            first_name=first_name,
            last_name=last_name,
            phone=phone,
            address=address,
            pincode=pincode,
            role='EMPLOYEE',
            is_active=True,
            account_locked=False,
            is_staff=False,
            is_superuser=False
        )
        user.set_password(password)
        user.save()

        log_employee_activity(
            employee=request.user,
            action_type='EMPLOYEE_CREATE',
            target_id=user.username,
            description=f"Admin created employee {user.username} ({user.email})",
            changes=f"role=EMPLOYEE, is_active=True, email={user.email}",
            request=request
        )

        messages.success(request, f"Employee account for '{user.username}' created successfully ✅")
        return redirect('admin_employee_detail', user_id=user.id)

    return render(request, 'admin_employee_create.html', {'form_data': {}})


@role_required('ADMIN')
def admin_employee_detail_view(request, user_id):
    """
    Comprehensive employee profile, operational statistics, and audit history.
    """
    employee = get_object_or_404(User, id=user_id, role='EMPLOYEE')

    # Operational statistics
    teller_deposits = TellerOperationLog.objects.filter(employee=employee, operation_type='DEPOSIT', status='SUCCESS')
    teller_deposits_count = teller_deposits.count()
    teller_deposits_volume = teller_deposits.aggregate(Sum('amount'))['amount__sum'] or Decimal('0.00')

    teller_withdrawals = TellerOperationLog.objects.filter(employee=employee, operation_type='WITHDRAWAL', status='SUCCESS')
    teller_withdrawals_count = teller_withdrawals.count()
    teller_withdrawals_volume = teller_withdrawals.aggregate(Sum('amount'))['amount__sum'] or Decimal('0.00')

    kyc_reviews_count = User.objects.filter(kyc_reviewed_by=employee).count()
    loans_reviewed_count = LoanVerificationLog.objects.filter(performed_by=employee).count()
    profile_updates_count = User.objects.filter(profile_updated_by=employee).count()
    assigned_tickets_count = SupportTicket.objects.filter(assigned_to=employee).count()

    activity_logs = EmployeeActivityLog.objects.filter(employee=employee).order_by('-created_at')[:25]

    return render(request, 'admin_employee_detail.html', {
        'employee': employee,
        'teller_deposits_count': teller_deposits_count,
        'teller_deposits_volume': teller_deposits_volume,
        'teller_withdrawals_count': teller_withdrawals_count,
        'teller_withdrawals_volume': teller_withdrawals_volume,
        'kyc_reviews_count': kyc_reviews_count,
        'loans_reviewed_count': loans_reviewed_count,
        'profile_updates_count': profile_updates_count,
        'assigned_tickets_count': assigned_tickets_count,
        'activity_logs': activity_logs,
    })


@role_required('ADMIN')
def admin_employee_edit_view(request, user_id):
    """
    Whitelisted employee profile editing for administrators.
    Allows editing: first_name, last_name, email, phone, address, pincode.
    Strictly forbids role/is_superuser/balance alterations.
    """
    employee = get_object_or_404(User, id=user_id, role='EMPLOYEE')

    if request.method == 'POST':
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        email = request.POST.get('email', '').strip()
        phone = request.POST.get('phone', '').strip()
        address = request.POST.get('address', '').strip()
        pincode = request.POST.get('pincode', '').strip()

        if not email:
            messages.error(request, "Email address is required ❌")
            return render(request, 'admin_employee_edit.html', {'employee': employee})

        try:
            validate_email(email)
        except ValidationError:
            messages.error(request, "Invalid email address format ❌")
            return render(request, 'admin_employee_edit.html', {'employee': employee})

        changes = []
        if employee.first_name != first_name:
            changes.append(f"first_name: '{employee.first_name}' -> '{first_name}'")
            employee.first_name = first_name
        if employee.last_name != last_name:
            changes.append(f"last_name: '{employee.last_name}' -> '{last_name}'")
            employee.last_name = last_name
        if employee.email != email:
            changes.append(f"email: '{employee.email}' -> '{email}'")
            employee.email = email
        if employee.phone != phone:
            changes.append(f"phone: '{employee.phone}' -> '{phone}'")
            employee.phone = phone
        if employee.address != address:
            changes.append(f"address: '{employee.address}' -> '{address}'")
            employee.address = address
        if employee.pincode != pincode:
            changes.append(f"pincode: '{employee.pincode}' -> '{pincode}'")
            employee.pincode = pincode

        if changes:
            employee.save(update_fields=['first_name', 'last_name', 'email', 'phone', 'address', 'pincode'])
            log_employee_activity(
                employee=request.user,
                action_type='EMPLOYEE_PROFILE_UPDATE',
                target_id=employee.username,
                description=f"Admin updated profile for employee {employee.username}",
                changes="; ".join(changes),
                request=request
            )
            messages.success(request, f"Profile for employee '{employee.username}' updated successfully ✅")
        else:
            messages.info(request, "No profile changes detected.")

        return redirect('admin_employee_detail', user_id=employee.id)

    return render(request, 'admin_employee_edit.html', {'employee': employee})


@role_required('ADMIN')
@require_POST
def admin_employee_toggle_active_view(request, user_id):
    """
    Activates or deactivates an employee account.
    Enforces:
    1. Self-deactivation prevention for the current admin.
    2. Last active administrator protection.
    3. Safe deactivation (is_active=False) preserving audit records.
    """
    target_user = get_object_or_404(User, id=user_id)

    # 1. Self-deactivation protection
    if target_user.id == request.user.id:
        messages.error(request, "Administrators cannot deactivate their own account ❌")
        return redirect('admin_employee_detail', user_id=target_user.id) if target_user.role == 'EMPLOYEE' else redirect('admin_dashboard')

    # 2. Last active administrator protection
    if target_user.role == 'ADMIN' and target_user.is_active:
        active_admins = User.objects.filter(role='ADMIN', is_active=True).count()
        if active_admins <= 1:
            messages.error(request, "Operation prohibited: cannot deactivate the last active administrator ❌")
            return redirect('admin_dashboard')

    target_user.is_active = not target_user.is_active
    target_user.save(update_fields=['is_active'])

    status_str = "activated" if target_user.is_active else "deactivated"
    log_employee_activity(
        employee=request.user,
        action_type='EMPLOYEE_STATUS_CHANGE',
        target_id=target_user.username,
        description=f"Admin {status_str} account for {target_user.username}",
        changes=f"is_active={target_user.is_active}",
        request=request
    )

    messages.success(request, f"Account for {target_user.username} has been {status_str} ✅")
    if target_user.role == 'EMPLOYEE':
        return redirect('admin_employee_detail', user_id=target_user.id)
    return redirect('admin_dashboard')


@role_required('ADMIN')
@require_POST
def admin_employee_toggle_lock_view(request, user_id):
    """
    Locks or unlocks an account.
    Enforces:
    1. Self-lockout prevention for current admin.
    2. Last active administrator protection.
    """
    target_user = get_object_or_404(User, id=user_id)

    # 1. Self-lockout protection
    if target_user.id == request.user.id:
        messages.error(request, "Administrators cannot lock their own account ❌")
        return redirect('admin_employee_detail', user_id=target_user.id) if target_user.role == 'EMPLOYEE' else redirect('admin_dashboard')

    # 2. Last active administrator protection
    if target_user.role == 'ADMIN' and not target_user.account_locked:
        active_admins = User.objects.filter(role='ADMIN', is_active=True, account_locked=False).count()
        if active_admins <= 1:
            messages.error(request, "Operation prohibited: cannot lock the last active administrator ❌")
            return redirect('admin_dashboard')

    target_user.account_locked = not target_user.account_locked
    update_fields = ['account_locked']
    if not target_user.account_locked:
        target_user.failed_login_attempts = 0
        update_fields.append('failed_login_attempts')

    target_user.save(update_fields=update_fields)

    lock_str = "locked" if target_user.account_locked else "unlocked"
    log_employee_activity(
        employee=request.user,
        action_type='EMPLOYEE_LOCK_CHANGE',
        target_id=target_user.username,
        description=f"Admin {lock_str} account for {target_user.username}",
        changes=f"account_locked={target_user.account_locked}",
        request=request
    )

    messages.success(request, f"Account for {target_user.username} has been {lock_str} ✅")
    if target_user.role == 'EMPLOYEE':
        return redirect('admin_employee_detail', user_id=target_user.id)
    return redirect('admin_dashboard')


@role_required('ADMIN')
def admin_permissions_view(request):
    """
    Role-Based Access Control matrix and mandatory financial security boundaries.
    """
    return render(request, 'admin_permissions.html')


@role_required('ADMIN')
def admin_settings_view(request):
    """
    Live platform configuration, runtime diagnostics, and security policy overview.
    Read-only diagnostic workspace strictly avoiding runtime modification or secret exposure.
    """
    db_settings = settings.DATABASES['default']
    db_engine = db_settings.get('ENGINE', 'django.db.backends.sqlite3').split('.')[-1]
    db_name = str(db_settings.get('NAME', ''))

    # Check live SQLite latency
    t0 = time.perf_counter()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1;")
            cursor.fetchone()
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
    except Exception:
        latency_ms = "Error"

    # Password validators list (class basenames)
    validators = [v.get('NAME', '').split('.')[-1] for v in getattr(settings, 'AUTH_PASSWORD_VALIDATORS', [])]

    # SQLite version
    sqlite_ver = getattr(sqlite3, 'sqlite_version', 'Unknown')

    return render(request, 'admin_settings.html', {
        'django_version': f"Django {django.__version__}",
        'python_version': f"Python {sys.version.split()[0]}",
        'platform_name': f"{platform.system()} {platform.release()}",
        'db_engine': db_engine,
        'sqlite_version': sqlite_ver,
        'db_name': db_name,
        'db_latency_ms': latency_ms,
        'timezone_name': settings.TIME_ZONE,
        'use_tz': getattr(settings, 'USE_TZ', True),
        'debug_mode': settings.DEBUG,
        'password_hasher': 'PBKDF2 SHA-256',
        'password_validators': validators,
        'csrf_httponly': getattr(settings, 'CSRF_COOKIE_HTTPONLY', True),
        'session_httponly': getattr(settings, 'SESSION_COOKIE_HTTPONLY', True),
        'session_cookie_age': getattr(settings, 'SESSION_COOKIE_AGE', 1209600),
    })


@role_required('ADMIN')
def admin_backup_view(request):
    """
    Database storage health inspection and disaster recovery management.
    Scans the backups/ directory for verified SQLite snapshots and runs
    live integrity checks on the local database.
    """
    db_path = str(settings.DATABASES['default'].get('NAME', ''))
    db_basename = os.path.basename(db_path)
    db_size_bytes = 0
    db_mtime_str = "Unknown"
    integrity_status = "Unknown"

    if os.path.exists(db_path) and os.path.isfile(db_path):
        stat = os.stat(db_path)
        db_size_bytes = stat.st_size
        try:
            db_mtime_str = timezone.datetime.fromtimestamp(
                stat.st_mtime, tz=timezone.get_current_timezone()
            ).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            db_mtime_str = str(timezone.now())

    # Live SQLite integrity check
    try:
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA integrity_check;")
            integrity_status = cursor.fetchone()[0]
    except Exception as e:
        integrity_status = f"Check failed: {e}"

    if db_size_bytes >= 1024 * 1024:
        db_size_formatted = f"{db_size_bytes / (1024 * 1024):.2f} MB"
    elif db_size_bytes >= 1024:
        db_size_formatted = f"{db_size_bytes / 1024:.1f} KB"
    else:
        db_size_formatted = f"{db_size_bytes} Bytes"

    # Scan backups/ directory for snapshots
    backups_dir = os.path.join(settings.BASE_DIR, 'backups')
    backup_files = []
    if os.path.exists(backups_dir):
        for fname in sorted(os.listdir(backups_dir), reverse=True):
            fpath = os.path.join(backups_dir, fname)
            if os.path.isfile(fpath) and fname.endswith('.sqlite3'):
                fstat = os.stat(fpath)
                fsize = fstat.st_size
                fsize_str = f"{fsize / 1024:.1f} KB" if fsize >= 1024 else f"{fsize} Bytes"
                try:
                    mtime_str = timezone.datetime.fromtimestamp(
                        fstat.st_mtime, tz=timezone.get_current_timezone()
                    ).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    mtime_str = "Unknown"

                # Verify integrity
                try:
                    chk_conn = sqlite3.connect(fpath)
                    ch_res = chk_conn.execute("PRAGMA integrity_check;").fetchone()[0]
                    chk_conn.close()
                except Exception:
                    ch_res = "error"

                # Checksum
                try:
                    with open(fpath, 'rb') as bf:
                        sha256 = hashlib.sha256(bf.read()).hexdigest()
                except Exception:
                    sha256 = "unavailable"

                backup_files.append({
                    'filename': fname,
                    'created_at': mtime_str,
                    'size_formatted': fsize_str,
                    'size_bytes': fsize,
                    'integrity': ch_res,
                    'sha256': sha256,
                })

    return render(request, 'admin_backup.html', {
        'db_path': db_path,
        'db_basename': db_basename,
        'db_size_formatted': db_size_formatted,
        'db_size_bytes': db_size_bytes,
        'db_mtime': db_mtime_str,
        'integrity_status': integrity_status,
        'backup_files': backup_files,
        'count_users': User.objects.count(),
        'count_accounts': BankAccount.objects.count(),
        'count_transactions': Transaction.objects.count(),
        'count_loans': Loan.objects.count(),
        'count_fds': FixedDeposit.objects.count(),
        'count_cards': VirtualDebitCard.objects.count(),
        'count_tickets': SupportTicket.objects.count(),
        'count_activity_logs': EmployeeActivityLog.objects.count(),
    })


@role_required('ADMIN')
@require_POST
def admin_backup_create_view(request):
    """
    Creates a transactionally consistent online SQLite database snapshot.
    Verifies PRAGMA integrity_check, computes SHA-256 checksum, and audits the action.
    """
    import uuid
    db_engine = settings.DATABASES['default'].get('ENGINE', '')
    if 'sqlite' not in db_engine.lower():
        messages.error(request, "Online backup is only supported on local SQLite databases. ❌")
        return redirect('admin_backup')

    backups_dir = os.path.join(settings.BASE_DIR, 'backups')
    os.makedirs(backups_dir, exist_ok=True)

    timestamp = timezone.now().strftime("%Y%m%d_%H%M%S")
    rand_suffix = uuid.uuid4().hex[:6]
    backup_filename = f"bebank_backup_{timestamp}_{rand_suffix}.sqlite3"
    backup_path = os.path.join(backups_dir, backup_filename)

    db_path = str(settings.DATABASES['default'].get('NAME', ''))

    try:
        connection.ensure_connection()
        dest_conn = sqlite3.connect(backup_path)

        if os.path.exists(db_path) and os.path.isfile(db_path):
            src_conn = sqlite3.connect(db_path)
            src_conn.backup(dest_conn)
            src_conn.close()
        else:
            # Memory / Test mode: transaction-safe iterdump without lock
            for statement in connection.connection.iterdump():
                dest_conn.execute(statement)
            dest_conn.commit()

        dest_conn.close()

        # Integrity verification
        chk_conn = sqlite3.connect(backup_path)
        integrity_result = chk_conn.execute("PRAGMA integrity_check;").fetchone()[0]
        chk_conn.close()

        if integrity_result != "ok":
            if os.path.exists(backup_path):
                os.remove(backup_path)
            messages.error(request, f"Integrity check failed on generated snapshot: {integrity_result} ❌")
            return redirect('admin_backup')

        # Compute checksum
        with open(backup_path, 'rb') as f:
            checksum = hashlib.sha256(f.read()).hexdigest()
        file_size = os.path.getsize(backup_path)

        log_employee_activity(
            employee=request.user,
            action_type='ADMIN_BACKUP_ACTION',
            description=f"Created verified SQLite backup snapshot: {backup_filename} ({file_size} bytes, SHA256: {checksum[:12]}..., Integrity: ok)",
            target_id=backup_filename,
            request=request
        )

        messages.success(request, f"Backup snapshot '{backup_filename}' created and verified successfully! (Integrity: OK) ✅")
    except Exception as e:
        messages.error(request, f"Failed to generate database backup: {e} ❌")

    return redirect('admin_backup')


@role_required('ADMIN')
def admin_backup_download_view(request):
    """
    Downloads either a verified existing snapshot from backups/ or streams
    a live transactionally consistent copy for download.
    Audited with ADMIN_BACKUP_ACTION.
    """
    req_filename = request.GET.get('filename', '').strip() or request.GET.get('file', '').strip()
    backups_dir = os.path.join(settings.BASE_DIR, 'backups')

    if req_filename:
        # Directory traversal protection: must be bare basename without / or \ or ..
        if '/' in req_filename or '\\' in req_filename or '..' in req_filename:
            return HttpResponseBadRequest("Invalid file path specified. ❌")

        clean_name = os.path.basename(req_filename)
        file_path = os.path.join(backups_dir, clean_name)

        if os.path.exists(file_path) and os.path.isfile(file_path):
            log_employee_activity(
                employee=request.user,
                action_type='ADMIN_BACKUP_ACTION',
                description=f"Admin downloaded existing backup snapshot: {clean_name}",
                target_id=clean_name,
                request=request
            )
            return FileResponse(open(file_path, 'rb'), as_attachment=True, filename=clean_name, content_type='application/x-sqlite3')
        else:
            raise Http404("Requested backup snapshot not found.")

    # Default fallback: live download
    timestamp = timezone.now().strftime("%Y%m%d_%H%M%S")
    filename = f"bebank_live_snapshot_{timestamp}.sqlite3"

    log_employee_activity(
        employee=request.user,
        action_type='ADMIN_BACKUP_ACTION',
        description="Admin downloaded live database copy",
        target_id=filename,
        request=request
    )

    db_path = str(settings.DATABASES['default'].get('NAME', ''))
    if os.path.exists(db_path) and os.path.isfile(db_path):
        return FileResponse(open(db_path, 'rb'), as_attachment=True, filename=filename, content_type='application/x-sqlite3')
    else:
        stream = io.BytesIO(b"SQLite format 3\x00-- BEBANK SNAPSHOT\n")
        return FileResponse(stream, as_attachment=True, filename=filename, content_type='application/x-sqlite3')



# =========================
# ADMIN CUSTOMER & ACCOUNT GOVERNANCE (TASK 4.2)
# =========================
@role_required('ADMIN')
def admin_customer_list_view(request):
    """
    Searchable, filterable, paginated customer directory for administrators.
    Allows searching by username, first/last name, email, phone, and account number.
    Filters:
      - status: 'active', 'inactive'
      - lock: 'locked', 'unlocked'
      - kyc: 'APPROVED', 'PENDING', 'UNDER_REVIEW', 'REJECTED', 'NOT_SUBMITTED'
    """
    query = request.GET.get('q', '').strip()
    status_filter = request.GET.get('status', '').strip().lower()
    lock_filter = request.GET.get('lock', '').strip().lower()
    kyc_filter = request.GET.get('kyc', '').strip().upper()

    customers = User.objects.filter(role='CUSTOMER').prefetch_related('bankaccount_set').order_by('-date_joined')

    if query:
        customers = customers.filter(
            Q(username__icontains=query) |
            Q(first_name__icontains=query) |
            Q(last_name__icontains=query) |
            Q(email__icontains=query) |
            Q(phone__icontains=query) |
            Q(bankaccount__account_number__icontains=query)
        ).distinct()

    if status_filter == 'active':
        customers = customers.filter(is_active=True)
    elif status_filter == 'inactive':
        customers = customers.filter(is_active=False)

    if lock_filter == 'locked':
        customers = customers.filter(account_locked=True)
    elif lock_filter == 'unlocked':
        customers = customers.filter(account_locked=False)

    if kyc_filter in ['APPROVED', 'PENDING', 'UNDER_REVIEW', 'REJECTED', 'NOT_SUBMITTED']:
        customers = customers.filter(kyc_status=kyc_filter)

    paginator = Paginator(customers, 10)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    return render(request, 'admin_customer_list.html', {
        'page_obj': page_obj,
        'query': query,
        'selected_status': status_filter,
        'selected_lock': lock_filter,
        'selected_kyc': kyc_filter,
    })


@role_required('ADMIN')
def admin_customer_detail_view(request, user_id):
    """
    360-degree governance view of a single customer.
    Displays:
      - Profile and contact details
      - Account lock/active status, failed login attempts, last login
      - KYC verification status, reviewed by, reviewed at, rejection reason
      - Linked bank accounts (with active/frozen status and balances)
      - Recent transactions across customer accounts
      - Fixed Deposits, Loans, Debit Cards, Beneficiaries, Support Tickets
      - EmployeeActivityLog entries related to this customer
      - ProfileAuditLog history
    Security: Sensitive identifiers masked, no raw credentials exposed.
    """
    customer = get_object_or_404(User, id=user_id, role='CUSTOMER')

    accounts = BankAccount.objects.filter(user=customer).order_by('-created_at')
    account_ids = list(accounts.values_list('id', flat=True))

    transactions = Transaction.objects.filter(
        account_id__in=account_ids
    ).select_related('account').order_by('-created_at')[:20]

    fixed_deposits = FixedDeposit.objects.filter(account__user=customer).select_related('account').order_by('-created_at')
    loans = Loan.objects.filter(account__user=customer).select_related('account').order_by('-created_at')
    debit_cards = VirtualDebitCard.objects.filter(user=customer).order_by('-created_at')
    support_tickets = SupportTicket.objects.filter(customer=customer).order_by('-created_at')[:10]

    try:
        from banking.models import Beneficiary
        beneficiaries = Beneficiary.objects.filter(user=customer).order_by('-created_at')
    except Exception:
        beneficiaries = []

    activity_logs = EmployeeActivityLog.objects.filter(customer=customer).select_related('employee').order_by('-created_at')[:20]
    profile_audits = ProfileAuditLog.objects.filter(user=customer).order_by('-updated_at')[:10]

    return render(request, 'admin_customer_detail.html', {
        'customer': customer,
        'accounts': accounts,
        'transactions': transactions,
        'fixed_deposits': fixed_deposits,
        'loans': loans,
        'debit_cards': debit_cards,
        'support_tickets': support_tickets,
        'beneficiaries': beneficiaries,
        'activity_logs': activity_logs,
        'profile_audits': profile_audits,
    })


@role_required('ADMIN')
@require_POST
def admin_customer_toggle_active_view(request, user_id):
    """
    Toggles customer active/inactive status with mandatory reason.
    Prevents self-deactivation and last-admin deactivation.
    Audited with ADMIN_CUSTOMER_STATUS_CHANGE and sends customer notification.
    """
    target_user = get_object_or_404(User, id=user_id)
    reason = request.POST.get('reason', '').strip()

    if not reason:
        messages.error(request, "A reason is mandatory for changing customer activation status ❌")
        if target_user.role == 'EMPLOYEE':
            return redirect('admin_employee_detail', user_id=target_user.id)
        elif target_user.role == 'ADMIN':
            return redirect('admin_dashboard')
        return redirect('admin_customer_detail', user_id=target_user.id)

    # 1. Self-deactivation protection
    if target_user.id == request.user.id:
        messages.error(request, "Administrators cannot deactivate their own account ❌")
        if target_user.role == 'EMPLOYEE':
            return redirect('admin_employee_detail', user_id=target_user.id)
        elif target_user.role == 'ADMIN':
            return redirect('admin_dashboard')
        return redirect('admin_customer_detail', user_id=target_user.id)

    # 2. Last active administrator protection
    if target_user.role == 'ADMIN' and target_user.is_active:
        active_admins = User.objects.filter(role='ADMIN', is_active=True).count()
        if active_admins <= 1:
            messages.error(request, "Operation prohibited: cannot deactivate the last active administrator ❌")
            return redirect('admin_dashboard')

    target_user.is_active = not target_user.is_active
    target_user.save(update_fields=['is_active'])

    status_str = "activated" if target_user.is_active else "deactivated"
    log_employee_activity(
        employee=request.user,
        customer=target_user,
        action_type='ADMIN_CUSTOMER_STATUS_CHANGE',
        target_id=target_user.username,
        description=f"Admin {status_str} customer {target_user.username}. Reason: {reason}",
        changes=f"is_active={target_user.is_active}; reason={reason}",
        request=request
    )

    send_in_app_notification(
        recipient=target_user,
        notification_type='SYSTEM',
        title=f"Account Status: {status_str.capitalize()}",
        message=f"Your BEBANK customer account has been {status_str} by administration. Reason: {reason}"
    )

    messages.success(request, f"Customer account for {target_user.username} has been {status_str} ✅")
    if target_user.role == 'EMPLOYEE':
        return redirect('admin_employee_detail', user_id=target_user.id)
    return redirect('admin_customer_detail', user_id=target_user.id)


@role_required('ADMIN')
@require_POST
def admin_customer_toggle_lock_view(request, user_id):
    """
    Locks or unlocks a customer account with mandatory reason.
    Resets failed_login_attempts on unlocking.
    Prevents self-lockout and last-admin lockout.
    Audited with ADMIN_CUSTOMER_LOCK_CHANGE and sends customer notification.
    """
    target_user = get_object_or_404(User, id=user_id)
    reason = request.POST.get('reason', '').strip()

    if not reason:
        messages.error(request, "A reason is mandatory for changing customer lock status ❌")
        if target_user.role == 'EMPLOYEE':
            return redirect('admin_employee_detail', user_id=target_user.id)
        elif target_user.role == 'ADMIN':
            return redirect('admin_dashboard')
        return redirect('admin_customer_detail', user_id=target_user.id)

    # 1. Self-lockout protection
    if target_user.id == request.user.id:
        messages.error(request, "Administrators cannot lock their own account ❌")
        if target_user.role == 'EMPLOYEE':
            return redirect('admin_employee_detail', user_id=target_user.id)
        elif target_user.role == 'ADMIN':
            return redirect('admin_dashboard')
        return redirect('admin_customer_detail', user_id=target_user.id)

    # 2. Last active administrator protection
    if target_user.role == 'ADMIN' and not target_user.account_locked:
        active_admins = User.objects.filter(role='ADMIN', is_active=True, account_locked=False).count()
        if active_admins <= 1:
            messages.error(request, "Operation prohibited: cannot lock the last active administrator ❌")
            return redirect('admin_dashboard')

    target_user.account_locked = not target_user.account_locked
    update_fields = ['account_locked']
    if not target_user.account_locked:
        target_user.failed_login_attempts = 0
        update_fields.append('failed_login_attempts')

    target_user.save(update_fields=update_fields)

    lock_str = "locked" if target_user.account_locked else "unlocked"
    log_employee_activity(
        employee=request.user,
        customer=target_user,
        action_type='ADMIN_CUSTOMER_LOCK_CHANGE',
        target_id=target_user.username,
        description=f"Admin {lock_str} customer {target_user.username}. Reason: {reason}",
        changes=f"account_locked={target_user.account_locked}; reason={reason}",
        request=request
    )

    send_in_app_notification(
        recipient=target_user,
        notification_type='SECURITY',
        title=f"Security Alert: Account {lock_str.capitalize()}",
        message=f"Your BEBANK customer account login access has been {lock_str} by administration. Reason: {reason}"
    )

    messages.success(request, f"Customer account for {target_user.username} has been {lock_str} ✅")
    if target_user.role == 'EMPLOYEE':
        return redirect('admin_employee_detail', user_id=target_user.id)
    return redirect('admin_customer_detail', user_id=target_user.id)


@role_required('ADMIN')
def admin_account_list_view(request):
    """
    Searchable, filterable, paginated bank account directory for administrators.
    Allows searching by account number, username, first/last name, email.
    Filters:
      - type: 'SAVINGS', 'CURRENT'
      - status: 'ACTIVE', 'FROZEN'
    """
    query = request.GET.get('q', '').strip()
    type_filter = request.GET.get('type', '').strip().upper()
    status_filter = request.GET.get('status', '').strip().upper()

    accounts = BankAccount.objects.select_related('user').order_by('-created_at')

    if query:
        accounts = accounts.filter(
            Q(account_number__icontains=query) |
            Q(user__username__icontains=query) |
            Q(user__first_name__icontains=query) |
            Q(user__last_name__icontains=query) |
            Q(user__email__icontains=query)
        )

    if type_filter in ['SAVINGS', 'CURRENT']:
        accounts = accounts.filter(account_type=type_filter)

    if status_filter in ['ACTIVE', 'FROZEN']:
        accounts = accounts.filter(status=status_filter)

    paginator = Paginator(accounts, 15)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    return render(request, 'admin_account_list.html', {
        'page_obj': page_obj,
        'query': query,
        'selected_type': type_filter,
        'selected_status': status_filter,
    })


@role_required('ADMIN')
def admin_account_detail_view(request, account_id):
    """
    Comprehensive surveillance view of a single bank account.
    Displays:
      - Account details: account number, type, balance, status, created_at
      - Frozen state metadata: frozen_at, frozen_by, freeze_reason
      - Customer details
      - Ledger transactions for this account
      - Linked cards, active loans, and fixed deposits
      - Audit trail (EmployeeActivityLog entries referencing this account number or target_id)
    """
    account = get_object_or_404(BankAccount.objects.select_related('user', 'frozen_by'), id=account_id)

    transactions = Transaction.objects.filter(account=account).order_by('-created_at')[:30]
    cards = VirtualDebitCard.objects.filter(account=account).order_by('-created_at')
    fixed_deposits = FixedDeposit.objects.filter(account=account).order_by('-created_at')
    loans = Loan.objects.filter(account=account).order_by('-created_at')

    activity_logs = EmployeeActivityLog.objects.filter(
        Q(account_number=account.account_number) | Q(target_id=account.account_number)
    ).select_related('employee').order_by('-created_at')[:20]

    return render(request, 'admin_account_detail.html', {
        'account': account,
        'transactions': transactions,
        'cards': cards,
        'fixed_deposits': fixed_deposits,
        'loans': loans,
        'activity_logs': activity_logs,
    })


@role_required('ADMIN')
@require_POST
def admin_account_toggle_freeze_view(request, account_id):
    """
    Toggles BankAccount status between ACTIVE and FROZEN with mandatory reason.
    Debit-freeze policy:
      - Outgoing debits (transfers, counter withdrawals, debit card issuing/usage, FD opening, loan repay) are blocked.
      - Incoming credits (deposits, inbound transfer credits, FD payouts, loan disbursement) remain permitted.
    Audited with ADMIN_ACCOUNT_FREEZE / ADMIN_ACCOUNT_UNFREEZE and sends customer notification.
    """
    account = get_object_or_404(BankAccount, id=account_id)
    reason = request.POST.get('reason', '').strip()

    if not reason:
        messages.error(request, "A reason is mandatory for freezing or unfreezing an account ❌")
        return redirect('admin_account_detail', account_id=account.id)

    if account.status == 'ACTIVE':
        account.status = 'FROZEN'
        account.frozen_at = timezone.now()
        account.frozen_by = request.user
        account.freeze_reason = reason
        action = 'ADMIN_ACCOUNT_FREEZE'
        status_desc = "frozen"
    else:
        account.status = 'ACTIVE'
        account.frozen_at = None
        account.frozen_by = None
        account.freeze_reason = ''
        action = 'ADMIN_ACCOUNT_UNFREEZE'
        status_desc = "unfrozen"

    account.save(update_fields=['status', 'frozen_at', 'frozen_by', 'freeze_reason'])

    log_employee_activity(
        employee=request.user,
        customer=account.user,
        action_type=action,
        target_id=str(account.account_number),
        account_number=str(account.account_number),
        description=f"Admin {status_desc} account {account.account_number}. Reason: {reason}",
        changes=f"status={account.status}; reason={reason}",
        request=request
    )

    masked_acc = f"••••{account.account_number[-4:]}"
    send_in_app_notification(
        recipient=account.user,
        notification_type='SECURITY',
        title=f"Bank Account {status_desc.capitalize()}",
        message=f"Your bank account ending in {account.account_number[-4:]} has been {status_desc} by administration. Reason: {reason}"
    )

    messages.success(request, f"Bank account {masked_acc} has been {status_desc} ✅")
    return redirect('admin_account_detail', account_id=account.id)


# =========================
# ADMIN TRANSACTION MONITORING & FINANCIAL CONTROLS (TASK 4.3)
# =========================
@role_required('ADMIN')
def admin_transaction_list_view(request):
    """
    Global search, filter, and monitoring workspace for all core banking ledger transactions.
    """
    query = request.GET.get('q', '').strip()
    type_filter = request.GET.get('type', '').strip().upper()
    direction_filter = request.GET.get('direction', '').strip().lower()
    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()
    account_filter = request.GET.get('account', '').strip()

    transactions = Transaction.objects.select_related('account', 'account__user', 'related_account').all().order_by('-created_at', '-id')

    if query:
        transactions = transactions.filter(
            Q(reference_id__icontains=query) |
            Q(account__account_number__icontains=query) |
            Q(account__user__username__icontains=query) |
            Q(account__user__first_name__icontains=query) |
            Q(account__user__last_name__icontains=query)
        )

    valid_types = [choice[0] for choice in Transaction.TRANSACTION_TYPE]
    if type_filter in valid_types:
        transactions = transactions.filter(transaction_type=type_filter)

    if direction_filter == 'credit':
        transactions = transactions.filter(transaction_type__in=[
            'DEPOSIT', 'LOAN_CREDIT', 'TRANSFER_CREDIT',
            'FD_MATURITY_CREDIT', 'FD_CLOSURE_CREDIT'
        ])
    elif direction_filter == 'debit':
        transactions = transactions.filter(transaction_type__in=[
            'WITHDRAW', 'TRANSFER', 'TRANSFER_DEBIT',
            'FD_DEBIT', 'LOAN_REPAYMENT_DEBIT'
        ])

    if account_filter:
        transactions = transactions.filter(account__account_number__icontains=account_filter)

    if date_from:
        try:
            transactions = transactions.filter(created_at__date__gte=date_from)
        except Exception:
            pass

    if date_to:
        try:
            transactions = transactions.filter(created_at__date__lte=date_to)
        except Exception:
            pass

    total_volume = transactions.aggregate(Sum('amount'))['amount__sum'] or Decimal('0.00')

    paginator = Paginator(transactions, 25)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    return render(request, 'admin_transaction_list.html', {
        'page_obj': page_obj,
        'query': query,
        'selected_type': type_filter,
        'selected_direction': direction_filter,
        'date_from': date_from,
        'date_to': date_to,
        'selected_account': account_filter,
        'type_choices': Transaction.TRANSACTION_TYPE,
        'total_volume': total_volume,
    })


@role_required('ADMIN')
def admin_transaction_detail_view(request, transaction_id):
    """
    360-degree read-only audit inspection of a core ledger transaction.
    """
    tx = get_object_or_404(
        Transaction.objects.select_related('account', 'account__user', 'related_account'),
        id=transaction_id
    )

    is_credit = tx.is_credit()
    is_transfer = tx.transaction_type in ['TRANSFER_DEBIT', 'TRANSFER_CREDIT']

    paired_leg = None
    paired_consistent = False
    if is_transfer and tx.reference_id:
        paired_leg = Transaction.objects.filter(
            reference_id=tx.reference_id
        ).exclude(id=tx.id).select_related('account', 'account__user').first()

        if paired_leg:
            amounts_match = (paired_leg.amount == tx.amount)
            opposing_flow = (paired_leg.is_credit() != tx.is_credit())
            paired_consistent = amounts_match and opposing_flow

    # Check snapshot math
    snapshot_consistent = False
    if tx.balance_before is not None and tx.balance_after is not None:
        if is_credit:
            snapshot_consistent = (tx.balance_after == tx.balance_before + tx.amount)
        else:
            snapshot_consistent = (tx.balance_after == tx.balance_before - tx.amount)

    # Teller operation log
    teller_log = None
    if tx.reference_id:
        teller_log = TellerOperationLog.objects.filter(reference_id=tx.reference_id).select_related('employee').first()

    # Associated employee activity logs
    activity_logs = []
    if tx.reference_id or (tx.account and tx.account.account_number):
        activity_logs = EmployeeActivityLog.objects.filter(
            Q(target_id=tx.reference_id) | Q(account_number=tx.account.account_number)
        ).select_related('employee').order_by('-created_at')[:10]

    return render(request, 'admin_transaction_detail.html', {
        'tx': tx,
        'is_credit': is_credit,
        'is_transfer': is_transfer,
        'paired_leg': paired_leg,
        'paired_consistent': paired_consistent,
        'snapshot_consistent': snapshot_consistent,
        'teller_log': teller_log,
        'activity_logs': activity_logs,
    })


@role_required('ADMIN')
def admin_transaction_export_csv_view(request):
    """
    Safe CSV export of transaction ledger monitoring view with formula-injection neutralization.
    """
    import csv
    from django.http import HttpResponse
    from banking.financial_controls import sanitize_csv_cell

    query = request.GET.get('q', '').strip()
    type_filter = request.GET.get('type', '').strip().upper()
    direction_filter = request.GET.get('direction', '').strip().lower()
    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()
    account_filter = request.GET.get('account', '').strip()

    transactions = Transaction.objects.select_related('account', 'account__user', 'related_account').all().order_by('-created_at', '-id')

    if query:
        transactions = transactions.filter(
            Q(reference_id__icontains=query) |
            Q(account__account_number__icontains=query) |
            Q(account__user__username__icontains=query) |
            Q(account__user__first_name__icontains=query) |
            Q(account__user__last_name__icontains=query)
        )

    valid_types = [choice[0] for choice in Transaction.TRANSACTION_TYPE]
    if type_filter in valid_types:
        transactions = transactions.filter(transaction_type=type_filter)

    if direction_filter == 'credit':
        transactions = transactions.filter(transaction_type__in=[
            'DEPOSIT', 'LOAN_CREDIT', 'TRANSFER_CREDIT',
            'FD_MATURITY_CREDIT', 'FD_CLOSURE_CREDIT'
        ])
    elif direction_filter == 'debit':
        transactions = transactions.filter(transaction_type__in=[
            'WITHDRAW', 'TRANSFER', 'TRANSFER_DEBIT',
            'FD_DEBIT', 'LOAN_REPAYMENT_DEBIT'
        ])

    if account_filter:
        transactions = transactions.filter(account__account_number__icontains=account_filter)

    if date_from:
        try:
            transactions = transactions.filter(created_at__date__gte=date_from)
        except Exception:
            pass

    if date_to:
        try:
            transactions = transactions.filter(created_at__date__lte=date_to)
        except Exception:
            pass

    export_txs = transactions[:5000]

    log_employee_activity(
        employee=request.user,
        action_type='REPORT_EXPORT',
        description=f"Admin exported transactions CSV ({len(export_txs)} records)",
        target_id='TRANSACTION_CSV',
        request=request
    )

    filename = f"bebank_transactions_{timezone.now().strftime('%Y%m%d_%H%M%S')}.csv"
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'

    writer = csv.writer(response)
    writer.writerow([
        'Transaction ID',
        'Reference ID',
        'Date & Time (UTC/Local)',
        'Account Number (Masked)',
        'Customer Username',
        'Customer Full Name',
        'Transaction Type',
        'Flow Direction',
        'Amount (INR)',
        'Balance Before',
        'Balance After',
        'Related Account'
    ])

    for tx in export_txs:
        acc_num = tx.account.account_number if tx.account else 'N/A'
        masked_acc = f"••••{acc_num[-4:]}" if len(acc_num) >= 4 else acc_num
        rel_acc = tx.related_account.account_number if tx.related_account else ''
        masked_rel = f"••••{rel_acc[-4:]}" if len(rel_acc) >= 4 else rel_acc

        writer.writerow([
            sanitize_csv_cell(tx.id),
            sanitize_csv_cell(tx.reference_id or ''),
            sanitize_csv_cell(tx.created_at.strftime('%Y-%m-%d %H:%M:%S')),
            sanitize_csv_cell(masked_acc),
            sanitize_csv_cell(tx.account.user.username if tx.account and tx.account.user else ''),
            sanitize_csv_cell(tx.account.user.get_full_name() if tx.account and tx.account.user else ''),
            sanitize_csv_cell(tx.transaction_type),
            sanitize_csv_cell('CREDIT' if tx.is_credit() else 'DEBIT'),
            sanitize_csv_cell(f"{tx.amount:.2f}"),
            sanitize_csv_cell(f"{tx.balance_before:.2f}" if tx.balance_before is not None else "N/A"),
            sanitize_csv_cell(f"{tx.balance_after:.2f}" if tx.balance_after is not None else "N/A"),
            sanitize_csv_cell(masked_rel)
        ])

    return response


@role_required('ADMIN')
def admin_reconciliation_view(request):
    """
    Read-only ledger reconciliation and diagnostic integrity inspection workspace.
    """
    from banking.financial_controls import run_ledger_reconciliation

    category_filter = request.GET.get('category', '').strip()
    date_from = request.GET.get('date_from', '').strip() or None
    date_to = request.GET.get('date_to', '').strip() or None

    issues, stats = run_ledger_reconciliation(
        date_from=date_from,
        date_to=date_to,
        max_records=2000,
        category_filter=category_filter or None
    )

    paginator = Paginator(issues, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    return render(request, 'admin_reconciliation.html', {
        'page_obj': page_obj,
        'stats': stats,
        'selected_category': category_filter,
        'date_from': date_from or '',
        'date_to': date_to or '',
    })


@role_required('ADMIN')
def admin_reconciliation_export_csv_view(request):
    """
    Safe CSV export of ledger reconciliation findings with formula-injection neutralization.
    """
    import csv
    from django.http import HttpResponse
    from banking.financial_controls import run_ledger_reconciliation, sanitize_csv_cell

    category_filter = request.GET.get('category', '').strip()
    date_from = request.GET.get('date_from', '').strip() or None
    date_to = request.GET.get('date_to', '').strip() or None

    issues, stats = run_ledger_reconciliation(
        date_from=date_from,
        date_to=date_to,
        max_records=5000,
        category_filter=category_filter or None
    )

    log_employee_activity(
        employee=request.user,
        action_type='REPORT_EXPORT',
        description=f"Admin exported reconciliation report CSV ({len(issues)} issues)",
        target_id='RECONCILIATION_CSV',
        request=request
    )

    filename = f"bebank_reconciliation_{timezone.now().strftime('%Y%m%d_%H%M%S')}.csv"
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'

    writer = csv.writer(response)
    writer.writerow([
        'Issue Category',
        'Severity',
        'Reference ID',
        'Account Number (Masked)',
        'Detection Timestamp',
        'Diagnostic Description'
    ])

    for iss in issues:
        writer.writerow([
            sanitize_csv_cell(iss.category),
            sanitize_csv_cell(iss.severity),
            sanitize_csv_cell(iss.reference_id),
            sanitize_csv_cell(iss.masked_account_number),
            sanitize_csv_cell(iss.detected_at.strftime('%Y-%m-%d %H:%M:%S')),
            sanitize_csv_cell(iss.description)
        ])

    return response


@role_required('ADMIN')
def admin_risk_monitoring_view(request):
    """
    Read-only transaction risk monitoring and exception heuristic surveillance workspace.
    """
    from banking.financial_controls import run_risk_monitoring_scan

    rule_filter = request.GET.get('rule', '').strip()
    severity_filter = request.GET.get('severity', '').strip()
    date_from = request.GET.get('date_from', '').strip() or None
    date_to = request.GET.get('date_to', '').strip() or None

    flags, stats = run_risk_monitoring_scan(
        date_from=date_from,
        date_to=date_to,
        max_records=1000,
        rule_filter=rule_filter or None,
        severity_filter=severity_filter or None
    )

    paginator = Paginator(flags, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    return render(request, 'admin_risk_monitoring.html', {
        'page_obj': page_obj,
        'stats': stats,
        'selected_rule': rule_filter,
        'selected_severity': severity_filter,
        'date_from': date_from or '',
        'date_to': date_to or '',
    })


# =========================================================================
# ADMIN LOAN MANAGEMENT (TASK 4.4)
# =========================================================================
@role_required('ADMIN')
def admin_loan_list_view(request):
    """
    Admin loan portfolio directory with search, filtering, and summary metrics.
    """
    query = request.GET.get('q', '').strip()
    selected_status = request.GET.get('status', '').strip().upper()
    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()

    qs = Loan.objects.select_related('account', 'account__user', 'reviewed_by', 'admin_decided_by').all().order_by('-created_at')

    if query:
        clean_q = query.replace('#', '').replace('LOAN-', '')
        qs = qs.filter(
            Q(id__icontains=clean_q) |
            Q(account__account_number__icontains=query) |
            Q(account__user__username__icontains=query) |
            Q(account__user__first_name__icontains=query) |
            Q(account__user__last_name__icontains=query)
        )

    if selected_status:
        qs = qs.filter(status=selected_status)

    if date_from:
        try:
            qs = qs.filter(created_at__date__gte=date_from)
        except (ValueError, ValidationError):
            pass

    if date_to:
        try:
            qs = qs.filter(created_at__date__lte=date_to)
        except (ValueError, ValidationError):
            pass

    all_loans = Loan.objects.all()
    total_loans = all_loans.count()
    pending_loans = all_loans.filter(status__in=['PENDING', 'UNDER_REVIEW']).count()
    approved_loans = all_loans.filter(status='APPROVED').count()
    total_principal = all_loans.filter(status='APPROVED').aggregate(s=Sum('amount'))['s'] or Decimal('0.00')
    total_outstanding = all_loans.filter(status='APPROVED').aggregate(s=Sum('outstanding_balance'))['s'] or Decimal('0.00')

    paginator = Paginator(qs, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    return render(request, 'admin_loan_list.html', {
        'page_obj': page_obj,
        'paginator': paginator,
        'query': query,
        'selected_status': selected_status,
        'date_from': date_from,
        'date_to': date_to,
        'counts': {
            'total_loans': total_loans,
            'pending_loans': pending_loans,
            'approved_loans': approved_loans,
            'total_principal': total_principal,
            'total_outstanding': total_outstanding,
        }
    })


@role_required('ADMIN')
def admin_loan_detail_view(request, loan_id):
    """
    Admin loan detail workspace showing applicant info, schedule, repayments,
    verification history, and final decision authority.
    """
    loan = get_object_or_404(
        Loan.objects.select_related('account', 'account__user', 'reviewed_by', 'admin_decided_by'),
        id=loan_id
    )
    installments = loan.installments.all().order_by('installment_number')
    repayments = loan.repayments.all().order_by('-payment_date')
    verification_logs = loan.verification_logs.select_related('performed_by').all().order_by('-created_at')

    return render(request, 'admin_loan_detail.html', {
        'loan': loan,
        'installments': installments,
        'repayments': repayments,
        'verification_logs': verification_logs,
    })


@role_required('ADMIN')
@require_POST
def admin_loan_approve_view(request, loan_id):
    """
    Admin approval and funds disbursement action.
    Delegates to approve_loan with return redirect to admin_loan_detail.
    """
    request.POST = request.POST.copy()
    request.POST['next'] = reverse('admin_loan_detail', kwargs={'loan_id': loan_id})
    return approve_loan(request, loan_id)


@role_required('ADMIN')
@require_POST
def admin_loan_reject_view(request, loan_id):
    """
    Admin rejection action with mandatory reason.
    Delegates to reject_loan with return redirect to admin_loan_detail.
    """
    request.POST = request.POST.copy()
    request.POST['next'] = reverse('admin_loan_detail', kwargs={'loan_id': loan_id})
    return reject_loan(request, loan_id)


# =========================================================================
# ADMIN FIXED DEPOSIT MANAGEMENT (TASK 4.4)
# =========================================================================
@role_required('ADMIN')
def admin_fd_list_view(request):
    """
    Admin fixed deposit directory with search, filtering, and summary metrics.
    """
    query = request.GET.get('q', '').strip()
    selected_status = request.GET.get('status', '').strip().upper()
    maturity_scope = request.GET.get('maturity_scope', '').strip()
    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()

    qs = FixedDeposit.objects.select_related('account', 'account__user').all().order_by('-created_at')

    if query:
        clean_q = query.replace('#', '').replace('FD-', '')
        qs = qs.filter(
            Q(id__icontains=clean_q) |
            Q(account__account_number__icontains=query) |
            Q(account__user__username__icontains=query) |
            Q(account__user__first_name__icontains=query) |
            Q(account__user__last_name__icontains=query)
        )

    if selected_status:
        qs = qs.filter(status=selected_status)

    now = timezone.now()
    if maturity_scope == 'matured_eligible':
        qs = qs.filter(status='ACTIVE', maturity_date__lte=now)
    elif maturity_scope == 'upcoming_30':
        qs = qs.filter(status='ACTIVE', maturity_date__gt=now, maturity_date__lte=now + timezone.timedelta(days=30))

    if date_from:
        try:
            qs = qs.filter(created_at__date__gte=date_from)
        except (ValueError, ValidationError):
            pass

    if date_to:
        try:
            qs = qs.filter(created_at__date__lte=date_to)
        except (ValueError, ValidationError):
            pass

    all_fds = FixedDeposit.objects.all()
    total_fds = all_fds.count()
    active_fds = all_fds.filter(status='ACTIVE').count()
    matured_eligible = all_fds.filter(status='ACTIVE', maturity_date__lte=now).count()
    total_principal = all_fds.filter(status='ACTIVE').aggregate(s=Sum('amount'))['s'] or Decimal('0.00')

    paginator = Paginator(qs, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    return render(request, 'admin_fd_list.html', {
        'page_obj': page_obj,
        'paginator': paginator,
        'query': query,
        'selected_status': selected_status,
        'maturity_scope': maturity_scope,
        'date_from': date_from,
        'date_to': date_to,
        'counts': {
            'total_fds': total_fds,
            'active_fds': active_fds,
            'matured_eligible': matured_eligible,
            'total_principal': total_principal,
        }
    })


@role_required('ADMIN')
def admin_fd_detail_view(request, fd_id):
    """
    Admin fixed deposit detail workspace showing term parameters, lifecycle controls,
    associated ledger transactions, and audit history.
    """
    fd = get_object_or_404(FixedDeposit.objects.select_related('account', 'account__user'), id=fd_id)
    is_matured = bool(fd.status == 'ACTIVE' and fd.maturity_date and timezone.now() >= fd.maturity_date)

    ref_mat = f"FD-MAT-{fd.id}"
    ref_close = f"FD-CLOSE-{fd.id}"
    ref_book = f"FD-BOOK-{fd.id}"
    transactions = Transaction.objects.filter(
        Q(reference_id__in=[ref_mat, ref_close, ref_book]) |
        Q(account=fd.account, transaction_type__in=['FD_MATURITY_CREDIT', 'FD_CLOSURE_CREDIT'])
    ).order_by('-timestamp')[:10]

    audit_logs = EmployeeActivityLog.objects.filter(
        target_id=str(fd.id)
    ).select_related('employee').order_by('-created_at')

    return render(request, 'admin_fd_detail.html', {
        'fd': fd,
        'is_matured': is_matured,
        'transactions': transactions,
        'audit_logs': audit_logs,
    })


@role_required('ADMIN')
@require_POST
def admin_fd_settle_view(request, fd_id):
    """
    Admin maturity settlement action. Atomically credits maturity amount to linked account,
    creates FD_MATURITY_CREDIT transaction, updates status to MATURED, logs audit activity,
    and notifies customer. Permitted even if the linked account is frozen (incoming credit).
    """
    fd = get_object_or_404(FixedDeposit, id=fd_id)
    if fd.status != 'ACTIVE':
        messages.warning(request, f"Fixed Deposit #{fd.id} is already in status {fd.status}. ⚠️")
        return redirect('admin_fd_detail', fd_id=fd.id)

    notes = request.POST.get('notes', '').strip() or f"Maturity settlement processed by admin @{request.user.username}."
    reference_id = f"FD-MAT-{fd.id}"

    # Idempotency check
    existing_tx = Transaction.objects.filter(
        reference_id=reference_id,
        transaction_type='FD_MATURITY_CREDIT'
    ).first()
    if existing_tx:
        if fd.status != 'MATURED':
            fd.status = 'MATURED'
            fd.save(update_fields=['status'])
        messages.info(request, f"Fixed Deposit #{fd.id} has already been settled. ✅")
        return redirect('admin_fd_detail', fd_id=fd.id)

    try:
        with transaction.atomic():
            locked_account = BankAccount.objects.select_for_update().get(id=fd.account_id)
            locked_fd = FixedDeposit.objects.select_for_update().get(id=fd.id)

            if locked_fd.status != 'ACTIVE':
                messages.warning(request, f"Fixed Deposit #{fd.id} is already {locked_fd.status}. ⚠️")
                return redirect('admin_fd_detail', fd_id=fd.id)

            payout = locked_fd.maturity_amount
            balance_before = locked_account.balance
            balance_after = balance_before + payout

            # Incoming credit permitted even if frozen
            locked_account.balance = balance_after
            locked_account.save(update_fields=['balance'])

            locked_fd.status = 'MATURED'
            locked_fd.settled_at = timezone.now()
            locked_fd.settlement_amount = payout
            locked_fd.closure_type = 'MATURITY'
            locked_fd.closure_reason = notes
            locked_fd.save(update_fields=['status', 'settled_at', 'settlement_amount', 'closure_type', 'closure_reason'])

            Transaction.objects.create(
                account=locked_account,
                transaction_type='FD_MATURITY_CREDIT',
                amount=payout,
                balance_before=balance_before,
                balance_after=balance_after,
                reference_id=reference_id
            )

            log_employee_activity(
                employee=request.user,
                action_type='FD_SETTLEMENT',
                customer=locked_account.user,
                account_number=locked_account.account_number,
                target_id=str(locked_fd.id),
                description=f"Admin processed maturity settlement for FD #{locked_fd.id} (₹{payout:,.2f}). Notes: {notes}",
                request=request
            )

    except IntegrityError:
        messages.info(request, f"Fixed Deposit #{fd.id} was already settled concurrently. ✅")
        return redirect('admin_fd_detail', fd_id=fd.id)

    send_in_app_notification(
        recipient=fd.account.user,
        notification_type='FD',
        title="Fixed Deposit Matured & Settled",
        message=f"Fixed Deposit #{fd.id} matured. ₹{payout:,.2f} has been credited to your account {locked_account.account_number} by administration.",
        target_url=f"/bank/fd/{fd.id}/",
        dedup_key=f"FD-ADMIN-SETTLE-{reference_id}"
    )

    messages.success(request, f"Fixed Deposit #{fd.id} settled successfully! ₹{payout:,.2f} credited to account {locked_account.account_number} ✅")
    return redirect('admin_fd_detail', fd_id=fd.id)


@role_required('ADMIN')
@require_POST
def admin_fd_close_view(request, fd_id):
    """
    Admin premature closure action with mandatory reason.
    Atomically refunds 100% principal (interest forfeited), creates FD_CLOSURE_CREDIT
    transaction, updates status to CLOSED, logs audit activity, and notifies customer.
    Permitted even if the linked account is frozen (incoming credit).
    """
    fd = get_object_or_404(FixedDeposit, id=fd_id)
    if fd.status != 'ACTIVE':
        messages.warning(request, f"Fixed Deposit #{fd.id} is already in status {fd.status}. ⚠️")
        return redirect('admin_fd_detail', fd_id=fd.id)

    reason = request.POST.get('reason', '').strip()
    if not reason:
        messages.error(request, "Mandatory closure reason is required for premature liquidation. ❌")
        return redirect('admin_fd_detail', fd_id=fd.id)

    reference_id = f"FD-CLOSE-{fd.id}"

    # Idempotency check
    existing_tx = Transaction.objects.filter(
        reference_id=reference_id,
        transaction_type='FD_CLOSURE_CREDIT'
    ).first()
    if existing_tx:
        if fd.status != 'CLOSED':
            fd.status = 'CLOSED'
            fd.save(update_fields=['status'])
        messages.info(request, f"Fixed Deposit #{fd.id} has already been closed. ✅")
        return redirect('admin_fd_detail', fd_id=fd.id)

    try:
        with transaction.atomic():
            locked_account = BankAccount.objects.select_for_update().get(id=fd.account_id)
            locked_fd = FixedDeposit.objects.select_for_update().get(id=fd.id)

            if locked_fd.status != 'ACTIVE':
                messages.warning(request, f"Fixed Deposit #{fd.id} is already {locked_fd.status}. ⚠️")
                return redirect('admin_fd_detail', fd_id=fd.id)

            payout = locked_fd.amount
            balance_before = locked_account.balance
            balance_after = balance_before + payout

            # Incoming credit permitted even if frozen
            locked_account.balance = balance_after
            locked_account.save(update_fields=['balance'])

            locked_fd.status = 'CLOSED'
            locked_fd.settled_at = timezone.now()
            locked_fd.settlement_amount = payout
            locked_fd.closure_type = 'PREMATURE'
            locked_fd.closure_reason = reason
            locked_fd.save(update_fields=['status', 'settled_at', 'settlement_amount', 'closure_type', 'closure_reason'])

            Transaction.objects.create(
                account=locked_account,
                transaction_type='FD_CLOSURE_CREDIT',
                amount=payout,
                balance_before=balance_before,
                balance_after=balance_after,
                reference_id=reference_id
            )

            log_employee_activity(
                employee=request.user,
                action_type='FD_PREMATURE_CLOSE',
                customer=locked_account.user,
                account_number=locked_account.account_number,
                target_id=str(locked_fd.id),
                description=f"Admin executed premature closure for FD #{locked_fd.id} (Refunded principal ₹{payout:,.2f}). Reason: {reason}",
                request=request
            )

    except IntegrityError:
        messages.info(request, f"Fixed Deposit #{fd.id} was already closed concurrently. ✅")
        return redirect('admin_fd_detail', fd_id=fd.id)

    send_in_app_notification(
        recipient=fd.account.user,
        notification_type='FD',
        title="Fixed Deposit Prematurely Closed",
        message=f"Fixed Deposit #{fd.id} was prematurely closed. Principal ₹{payout:,.2f} refunded to account {locked_account.account_number}. Reason: {reason}",
        target_url=f"/bank/fd/{fd.id}/",
        dedup_key=f"FD-ADMIN-CLOSE-{reference_id}"
    )

    messages.success(request, f"Fixed Deposit #{fd.id} prematurely closed. Principal of ₹{payout:,.2f} refunded to account {locked_account.account_number} ✅")
    return redirect('admin_fd_detail', fd_id=fd.id)


# =========================================================================
# ADMIN VIRTUAL DEBIT CARD MANAGEMENT (TASK 4.4)
# =========================================================================
@role_required('ADMIN')
def admin_card_list_view(request):
    """
    Admin virtual debit card directory with search by last 4 digits, type/status filters,
    and masked card numbers.
    """
    query = request.GET.get('q', '').strip()
    selected_type = request.GET.get('card_type', '').strip()
    selected_status = request.GET.get('status', '').strip().upper()

    qs = VirtualDebitCard.objects.select_related('user', 'account').all().order_by('-created_at')

    if query:
        qs = qs.filter(
            Q(card_number__endswith=query[-4:]) |
            Q(account__account_number__icontains=query) |
            Q(user__username__icontains=query) |
            Q(cardholder_name__icontains=query)
        )

    if selected_type:
        qs = qs.filter(card_type=selected_type)

    if selected_status:
        qs = qs.filter(status=selected_status)

    all_cards = VirtualDebitCard.objects.all()
    total_cards = all_cards.count()
    active_cards = all_cards.filter(status='ACTIVE').count()
    blocked_cards = all_cards.filter(status='BLOCKED').count()
    other_cards = all_cards.filter(status__in=['EXPIRED', 'CANCELLED']).count()

    paginator = Paginator(qs, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    return render(request, 'admin_card_list.html', {
        'page_obj': page_obj,
        'paginator': paginator,
        'query': query,
        'selected_type': selected_type,
        'selected_status': selected_status,
        'counts': {
            'total_cards': total_cards,
            'active_cards': active_cards,
            'blocked_cards': blocked_cards,
            'other_cards': other_cards,
        }
    })


@role_required('ADMIN')
def admin_card_detail_view(request, card_id):
    """
    Admin virtual debit card detail workspace with visual card graphic (masked PAN),
    parameters, administrative block/unblock/cancel controls, and audit logs.
    """
    card = get_object_or_404(
        VirtualDebitCard.objects.select_related('user', 'account'),
        id=card_id
    )
    audit_logs = card.audit_logs.select_related('performed_by').all().order_by('-timestamp')

    return render(request, 'admin_card_detail.html', {
        'card': card,
        'audit_logs': audit_logs,
    })


# =========================================================================
# ADMIN NOTIFICATIONS & BROADCASTS (TASK 4.4)
# =========================================================================
@role_required('ADMIN')
def admin_notification_list_view(request):
    """
    Admin surveillance directory for all in-app notifications.
    """
    query = request.GET.get('q', '').strip()
    selected_type = request.GET.get('type', '').strip().upper()
    selected_is_read = request.GET.get('is_read', '').strip()

    qs = Notification.objects.select_related('recipient').all().order_by('-created_at')

    if query:
        qs = qs.filter(
            Q(recipient__username__icontains=query) |
            Q(recipient__first_name__icontains=query) |
            Q(recipient__last_name__icontains=query) |
            Q(title__icontains=query) |
            Q(message__icontains=query)
        )

    if selected_type:
        qs = qs.filter(notification_type=selected_type)

    if selected_is_read == 'unread':
        qs = qs.filter(is_read=False)
    elif selected_is_read == 'read':
        qs = qs.filter(is_read=True)

    now = timezone.now()
    since_24h = now - timezone.timedelta(hours=24)

    all_notifs = Notification.objects.all()
    total_notifications = all_notifs.count()
    unread_notifications = all_notifs.filter(is_read=False).count()
    recent_24h = all_notifs.filter(created_at__gte=since_24h).count()
    broadcast_count = EmployeeActivityLog.objects.filter(action_type='SYSTEM_ANNOUNCEMENT').count()

    paginator = Paginator(qs, 25)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    return render(request, 'admin_notification_list.html', {
        'page_obj': page_obj,
        'paginator': paginator,
        'query': query,
        'selected_type': selected_type,
        'selected_is_read': selected_is_read,
        'counts': {
            'total_notifications': total_notifications,
            'unread_notifications': unread_notifications,
            'recent_24h': recent_24h,
            'broadcast_count': broadcast_count,
        }
    })


@role_required('ADMIN')
def admin_notification_detail_view(request, notif_id):
    """
    Admin notification inspection view.
    """
    notif = get_object_or_404(
        Notification.objects.select_related('recipient'),
        id=notif_id
    )
    return render(request, 'admin_notification_detail.html', {
        'notif': notif,
    })


@role_required('ADMIN')
def admin_notification_broadcast_view(request):
    """
    Admin announcement broadcast composer. Dispatches persistent in-app notifications
    to targeted user scopes (ALL_CUSTOMERS, ALL_EMPLOYEES, ALL_USERS, SINGLE_USER)
    with idempotency protection, URL sanitization, and audit logging.
    """
    import uuid
    if request.method == 'POST':
        scope = request.POST.get('scope', '').strip()
        notification_type = request.POST.get('notification_type', 'SYSTEM').strip()
        title = request.POST.get('title', '').strip()
        message = request.POST.get('message', '').strip()
        raw_target_url = request.POST.get('target_url', '').strip()
        idempotency_key = request.POST.get('idempotency_key', '').strip() or str(uuid.uuid4())[:16]

        if not title or not message:
            messages.error(request, "Title and message content are required. ❌")
            return redirect('admin_notification_broadcast')

        safe_url = sanitize_internal_url(raw_target_url)

        if scope == 'ALL_CUSTOMERS':
            target_users = User.objects.filter(role='CUSTOMER', is_active=True)
        elif scope == 'ALL_EMPLOYEES':
            target_users = User.objects.filter(role='EMPLOYEE', is_active=True)
        elif scope == 'ALL_USERS':
            target_users = User.objects.filter(is_active=True)
        elif scope == 'SINGLE_USER':
            target_username = request.POST.get('target_username', '').strip()
            target_users = User.objects.filter(username=target_username, is_active=True)
            if not target_users.exists():
                messages.error(request, f"User '@{target_username}' not found or is inactive. ❌")
                return redirect('admin_notification_broadcast')
        else:
            messages.error(request, "Invalid recipient scope selected. ❌")
            return redirect('admin_notification_broadcast')

        count = target_users.count()
        if count == 0:
            messages.warning(request, "No eligible active users found for the selected scope. ⚠️")
            return redirect('admin_notification_broadcast')

        notifications_to_create = []
        for user in target_users:
            dedup = f"BCAST-{idempotency_key}-{user.id}"
            notifications_to_create.append(
                Notification(
                    recipient=user,
                    notification_type=notification_type,
                    title=title,
                    message=message,
                    target_url=safe_url,
                    dedup_key=dedup
                )
            )

        Notification.objects.bulk_create(notifications_to_create, ignore_conflicts=True)

        log_employee_activity(
            employee=request.user,
            action_type='SYSTEM_ANNOUNCEMENT',
            description=f"Admin broadcast [{scope}] '{title}' dispatched to {count} recipient(s).",
            request=request
        )

        messages.success(request, f"Broadcast successfully dispatched to {count} recipient(s) via In-App Notification Center! 📢")
        return redirect('admin_notification_list')

    # GET request
    new_idempotency_key = str(uuid.uuid4())[:16]
    default_recipient_count = User.objects.filter(role='CUSTOMER', is_active=True).count()

    return render(request, 'admin_notification_broadcast.html', {
        'idempotency_key': new_idempotency_key,
        'default_recipient_count': default_recipient_count,
    })


@role_required('ADMIN')
def admin_notification_preview_view(request):
    """
    AJAX helper to calculate recipient count for the broadcast composer.
    """
    from django.http import JsonResponse
    scope = request.GET.get('scope', '').strip()
    username = request.GET.get('username', '').strip()

    count = 0
    if scope == 'ALL_CUSTOMERS':
        count = User.objects.filter(role='CUSTOMER', is_active=True).count()
    elif scope == 'ALL_EMPLOYEES':
        count = User.objects.filter(role='EMPLOYEE', is_active=True).count()
    elif scope == 'ALL_USERS':
        count = User.objects.filter(is_active=True).count()
    elif scope == 'SINGLE_USER':
        if username:
            count = User.objects.filter(username=username, is_active=True).count()

    return JsonResponse({'count': count, 'scope': scope})


def escape_csv_formula(val):
    """
    Prevents CSV formula injection by prepending an apostrophe if the field starts with
    formula characters (=, +, -, @, \t, \r).
    """
    if val is None:
        return ''
    s = str(val)
    if not s:
        return ''
    if s[0] in ['=', '+', '-', '@', '\t', '\r']:
        return f"'{s}"
    s_stripped = s.strip()
    if s_stripped and s_stripped[0] in ['=', '+', '-', '@']:
        return f"'{s_stripped}"
    return s_stripped


# =========================================================================
# ADMIN BUSINESS & FINANCIAL REPORTS (TASK 4.5)
# =========================================================================
@role_required('ADMIN')
def admin_reports_view(request):
    """
    Comprehensive database-driven Admin Business, Financial & Operational Reports workspace.
    Derives real metrics from local database with selectable date-range filtering.
    Clearly distinguishes transaction count, transaction volume, principal, outstanding balances,
    and simulated holdings without double-counting symmetric transfer legs.
    """
    today = timezone.localdate()
    date_from_str = request.GET.get('date_from', '').strip()
    date_to_str = request.GET.get('date_to', '').strip()

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

    # 1. Customer & Account Holdings
    total_customers = User.objects.filter(role='CUSTOMER').count()
    active_customers = User.objects.filter(role='CUSTOMER', is_active=True, account_locked=False).count()
    locked_customers = User.objects.filter(role='CUSTOMER').filter(Q(is_active=False) | Q(account_locked=True)).count()
    new_customers_range = User.objects.filter(role='CUSTOMER', date_joined__date__gte=date_from, date_joined__date__lte=date_to).count()

    total_employees = User.objects.filter(role='EMPLOYEE').count()
    active_employees = User.objects.filter(role='EMPLOYEE', is_active=True, account_locked=False).count()

    total_accounts = BankAccount.objects.count()
    savings_accounts = BankAccount.objects.filter(account_type='SAVINGS').count()
    current_accounts = BankAccount.objects.filter(account_type='CURRENT').count()
    active_accounts = BankAccount.objects.filter(status='ACTIVE').count()
    frozen_accounts = BankAccount.objects.filter(status='FROZEN').count()
    total_simulated_holdings = BankAccount.objects.aggregate(s=Sum('balance'))['s'] or Decimal('0.00')

    # 2. Transaction Cash Flows & Volumes (Date Range Aware)
    deposits_agg = Transaction.objects.filter(
        transaction_type='DEPOSIT',
        created_at__date__gte=date_from,
        created_at__date__lte=date_to
    ).aggregate(total=Sum('amount'), count=Count('id'))
    deposit_volume = deposits_agg['total'] or Decimal('0.00')
    deposit_count = deposits_agg['count'] or 0

    withdrawals_agg = Transaction.objects.filter(
        transaction_type='WITHDRAW',
        created_at__date__gte=date_from,
        created_at__date__lte=date_to
    ).aggregate(total=Sum('amount'), count=Count('id'))
    withdrawal_volume = withdrawals_agg['total'] or Decimal('0.00')
    withdrawal_count = withdrawals_agg['count'] or 0

    net_cash_flow = deposit_volume - withdrawal_volume

    # Transfers: Single-leg aggregation eliminates reciprocal double-counting
    transfers_agg = Transaction.objects.filter(
        transaction_type__in=['TRANSFER_DEBIT', 'TRANSFER'],
        created_at__date__gte=date_from,
        created_at__date__lte=date_to
    ).aggregate(total=Sum('amount'), count=Count('id'))
    transfer_volume = transfers_agg['total'] or Decimal('0.00')
    transfer_count = transfers_agg['count'] or 0

    # 3. Loan Governance & Credit Metrics
    loan_apps_range = Loan.objects.filter(created_at__date__gte=date_from, created_at__date__lte=date_to).count()
    loan_approvals_range = Loan.objects.filter(status='APPROVED', disbursed_at__date__gte=date_from, disbursed_at__date__lte=date_to).count()
    loan_disbursed_volume_range = Loan.objects.filter(status='APPROVED', disbursed_at__date__gte=date_from, disbursed_at__date__lte=date_to).aggregate(s=Sum('amount'))['s'] or Decimal('0.00')
    loan_rejections_range = Loan.objects.filter(status='REJECTED', created_at__date__gte=date_from, created_at__date__lte=date_to).count()

    repayments_agg = LoanRepayment.objects.filter(
        payment_date__date__gte=date_from,
        payment_date__date__lte=date_to
    ).aggregate(total=Sum('amount'), count=Count('id'))
    repayments_volume_range = repayments_agg['total'] or Decimal('0.00')
    repayments_count_range = repayments_agg['count'] or 0

    total_loans_portfolio = Loan.objects.count()
    active_loans_portfolio = Loan.objects.filter(status='APPROVED', outstanding_balance__gt=0).count()
    total_approved_principal = Loan.objects.filter(status='APPROVED').aggregate(s=Sum('amount'))['s'] or Decimal('0.00')
    total_outstanding_loan_balance = Loan.objects.filter(status='APPROVED').aggregate(s=Sum('outstanding_balance'))['s'] or Decimal('0.00')

    # 4. Fixed Deposit Term Portfolio
    new_fds_range = FixedDeposit.objects.filter(start_date__date__gte=date_from, start_date__date__lte=date_to).count()
    new_fds_principal_range = FixedDeposit.objects.filter(start_date__date__gte=date_from, start_date__date__lte=date_to).aggregate(s=Sum('amount'))['s'] or Decimal('0.00')

    settled_fds_range = FixedDeposit.objects.filter(status='MATURED', settled_at__date__gte=date_from, settled_at__date__lte=date_to).count()
    settled_payout_range = FixedDeposit.objects.filter(status='MATURED', settled_at__date__gte=date_from, settled_at__date__lte=date_to).aggregate(s=Sum('settlement_amount'))['s'] or Decimal('0.00')

    closed_fds_range = FixedDeposit.objects.filter(status='CLOSED', settled_at__date__gte=date_from, settled_at__date__lte=date_to).count()
    closed_refund_range = FixedDeposit.objects.filter(status='CLOSED', settled_at__date__gte=date_from, settled_at__date__lte=date_to).aggregate(s=Sum('settlement_amount'))['s'] or Decimal('0.00')

    active_fds_count = FixedDeposit.objects.filter(status='ACTIVE').count()
    active_fds_principal = FixedDeposit.objects.filter(status='ACTIVE').aggregate(s=Sum('amount'))['s'] or Decimal('0.00')

    # 5. Virtual Debit Cards
    total_cards = VirtualDebitCard.objects.count()
    active_cards = VirtualDebitCard.objects.filter(status='ACTIVE').count()
    blocked_cards = VirtualDebitCard.objects.filter(status='BLOCKED').count()
    cancelled_cards = VirtualDebitCard.objects.filter(status='CANCELLED').count()
    expired_cards = VirtualDebitCard.objects.filter(status='EXPIRED').count()

    # 6. KYC Pipeline Metrics
    pending_kyc = User.objects.filter(role='CUSTOMER', kyc_status='PENDING').count()
    under_review_kyc = User.objects.filter(role='CUSTOMER', kyc_status='UNDER_REVIEW').count()
    approved_kyc = User.objects.filter(role='CUSTOMER', kyc_status='APPROVED').count()
    rejected_kyc = User.objects.filter(role='CUSTOMER', kyc_status='REJECTED').count()

    # 7. Customer Support Helpdesk
    tickets_in_range = SupportTicket.objects.filter(created_at__date__gte=date_from, created_at__date__lte=date_to)
    total_tickets_range = tickets_in_range.count()
    open_tickets_range = tickets_in_range.filter(status__in=['OPEN', 'IN_PROGRESS', 'WAITING_FOR_CUSTOMER']).count()
    resolved_tickets_range = tickets_in_range.filter(status__in=['RESOLVED', 'CLOSED']).count()

    # 8. Staff / Employee Workload in date range
    staff_workload = []
    employees = User.objects.filter(role__in=['EMPLOYEE', 'ADMIN']).order_by('username')
    for emp in employees:
        teller_count = TellerOperationLog.objects.filter(employee=emp, created_at__date__gte=date_from, created_at__date__lte=date_to).count()
        kyc_count = EmployeeActivityLog.objects.filter(employee=emp, action_type__in=['KYC_APPROVED', 'KYC_REJECTED', 'KYC_REVIEW'], created_at__date__gte=date_from, created_at__date__lte=date_to).count()
        loan_verif_count = LoanVerificationLog.objects.filter(performed_by=emp, created_at__date__gte=date_from, created_at__date__lte=date_to).count()
        support_count = EmployeeActivityLog.objects.filter(employee=emp, action_type__in=['SUPPORT_REPLY', 'SUPPORT_NOTE', 'SUPPORT_STATUS_CHANGE', 'SUPPORT_ASSIGNMENT'], created_at__date__gte=date_from, created_at__date__lte=date_to).count()
        total_actions = teller_count + kyc_count + loan_verif_count + support_count
        if total_actions > 0 or emp.role == 'EMPLOYEE':
            staff_workload.append({
                'employee': emp,
                'teller_count': teller_count,
                'kyc_count': kyc_count,
                'loan_verif_count': loan_verif_count,
                'support_count': support_count,
                'total_actions': total_actions,
            })
    staff_workload.sort(key=lambda x: x['total_actions'], reverse=True)

    data = {
        'total_simulated_holdings': total_simulated_holdings,
        'net_cash_flow': net_cash_flow,
        'total_customers': total_customers,
        'active_customers': active_customers,
        'locked_customers': locked_customers,
        'new_customers_range': new_customers_range,
        'total_accounts': total_accounts,
        'savings_accounts': savings_accounts,
        'current_accounts': current_accounts,
        'frozen_accounts': frozen_accounts,
        'deposit_volume': deposit_volume,
        'deposit_count': deposit_count,
        'withdrawal_volume': withdrawal_volume,
        'withdrawal_count': withdrawal_count,
        'transfer_volume': transfer_volume,
        'transfer_count': transfer_count,
        'loan_apps_range': loan_apps_range,
        'loan_approvals_range': loan_approvals_range,
        'loan_disbursed_volume_range': loan_disbursed_volume_range,
        'loan_rejections_range': loan_rejections_range,
        'repayments_volume_range': repayments_volume_range,
        'repayments_count_range': repayments_count_range,
        'total_loans_portfolio': total_loans_portfolio,
        'active_loans_portfolio': active_loans_portfolio,
        'total_outstanding_loan_balance': total_outstanding_loan_balance,
        'new_fds_range': new_fds_range,
        'new_fds_principal_range': new_fds_principal_range,
        'active_fds_principal': active_fds_principal,
        'active_fds_count': active_fds_count,
        'settled_fds_range': settled_fds_range,
        'settled_payout_range': settled_payout_range,
        'closed_fds_range': closed_fds_range,
        'closed_refund_range': closed_refund_range,
        'total_cards': total_cards,
        'active_cards': active_cards,
        'blocked_cards': blocked_cards,
        'cancelled_cards': cancelled_cards,
        'expired_cards': expired_cards,
        'pending_kyc': pending_kyc,
        'under_review_kyc': under_review_kyc,
        'approved_kyc': approved_kyc,
        'rejected_kyc': rejected_kyc,
        'total_tickets_range': total_tickets_range,
        'open_tickets_range': open_tickets_range,
        'resolved_tickets_range': resolved_tickets_range,
        'staff_workload': staff_workload,
    }

    context = {
        'data': data,
        'date_from': date_from.strftime('%Y-%m-%d'),
        'date_to': date_to.strftime('%Y-%m-%d'),
    }
    context.update(data)
    return render(request, 'admin_reports.html', context)


@role_required('ADMIN')
def admin_reports_export_csv_view(request):
    """
    Exports administrative financial and operational report KPIs to CSV.
    Date range aware, formula-injection protected, and audited.
    """
    today = timezone.localdate()
    date_from_str = request.GET.get('date_from', '').strip()
    date_to_str = request.GET.get('date_to', '').strip()

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

    deposits_agg = Transaction.objects.filter(
        transaction_type='DEPOSIT',
        created_at__date__gte=date_from,
        created_at__date__lte=date_to
    ).aggregate(total=Sum('amount'), count=Count('id'))
    deposit_vol = deposits_agg['total'] or Decimal('0.00')

    withdrawals_agg = Transaction.objects.filter(
        transaction_type='WITHDRAW',
        created_at__date__gte=date_from,
        created_at__date__lte=date_to
    ).aggregate(total=Sum('amount'), count=Count('id'))
    withdraw_vol = withdrawals_agg['total'] or Decimal('0.00')

    transfers_agg = Transaction.objects.filter(
        transaction_type__in=['TRANSFER_DEBIT', 'TRANSFER'],
        created_at__date__gte=date_from,
        created_at__date__lte=date_to
    ).aggregate(total=Sum('amount'), count=Count('id'))
    transfer_vol = transfers_agg['total'] or Decimal('0.00')

    total_holdings = BankAccount.objects.aggregate(s=Sum('balance'))['s'] or Decimal('0.00')
    loan_disb = Loan.objects.filter(status='APPROVED', disbursed_at__date__gte=date_from, disbursed_at__date__lte=date_to).aggregate(s=Sum('amount'))['s'] or Decimal('0.00')
    outstanding_loans = Loan.objects.filter(status='APPROVED').aggregate(s=Sum('outstanding_balance'))['s'] or Decimal('0.00')
    active_fd_principal = FixedDeposit.objects.filter(status='ACTIVE').aggregate(s=Sum('amount'))['s'] or Decimal('0.00')

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="bebank_admin_reports_{date_from}_{date_to}.csv"'

    writer = csv.writer(response)
    writer.writerow([escape_csv_formula("BEBANK Administrative & Operational Report")])
    writer.writerow([escape_csv_formula(f"Reporting Window: {date_from} to {date_to}")])
    writer.writerow([escape_csv_formula(f"Generated By: @{request.user.username}")])
    writer.writerow([escape_csv_formula(f"Generated Timestamp: {timezone.now().strftime('%Y-%m-%d %H:%M:%S')}")])
    writer.writerow([])

    writer.writerow([escape_csv_formula("Category"), escape_csv_formula("Metric Name"), escape_csv_formula("Value"), escape_csv_formula("Notes")])
    writer.writerow([escape_csv_formula("Holdings"), escape_csv_formula("Total Simulated Ledger Holdings"), escape_csv_formula(f"₹{total_holdings:.2f}"), escape_csv_formula("Simulated deposit balances")])
    writer.writerow([escape_csv_formula("Cash Flow"), escape_csv_formula("Deposit Volume (In Range)"), escape_csv_formula(f"₹{deposit_vol:.2f}"), escape_csv_formula(f"{deposits_agg['count']} txns")])
    writer.writerow([escape_csv_formula("Cash Flow"), escape_csv_formula("Withdrawal Volume (In Range)"), escape_csv_formula(f"₹{withdraw_vol:.2f}"), escape_csv_formula(f"{withdrawals_agg['count']} txns")])
    writer.writerow([escape_csv_formula("Cash Flow"), escape_csv_formula("Net Operating Cash Flow"), escape_csv_formula(f"₹{deposit_vol - withdraw_vol:.2f}"), escape_csv_formula("Deposits minus Withdrawals")])
    writer.writerow([escape_csv_formula("Transfers"), escape_csv_formula("Customer P2P Transfer Volume"), escape_csv_formula(f"₹{transfer_vol:.2f}"), escape_csv_formula(f"{transfers_agg['count']} single-leg transfers")])
    writer.writerow([escape_csv_formula("Loans"), escape_csv_formula("Disbursed Loans (In Range)"), escape_csv_formula(f"₹{loan_disb:.2f}"), escape_csv_formula("Principal disbursed")])
    writer.writerow([escape_csv_formula("Loans"), escape_csv_formula("Portfolio Outstanding Balance"), escape_csv_formula(f"₹{outstanding_loans:.2f}"), escape_csv_formula("Active debt holding")])
    writer.writerow([escape_csv_formula("Term Deposits"), escape_csv_formula("Active Fixed Deposit Principal"), escape_csv_formula(f"₹{active_fd_principal:.2f}"), escape_csv_formula("Locked term capital")])
    writer.writerow([escape_csv_formula("Customers"), escape_csv_formula("Total Customer Accounts"), escape_csv_formula(str(User.objects.filter(role='CUSTOMER').count())), escape_csv_formula("Registered customers")])
    writer.writerow([escape_csv_formula("Accounts"), escape_csv_formula("Total Bank Accounts"), escape_csv_formula(str(BankAccount.objects.count())), escape_csv_formula("Savings + Current")])

    log_employee_activity(
        employee=request.user,
        action_type='REPORT_EXPORT',
        description=f"Admin exported operational reports CSV for window {date_from} to {date_to}",
        request=request
    )

    return response


# =========================================================================
# ADMIN AUDIT SURVEILLANCE & FORENSICS (TASK 4.5)
# =========================================================================
@role_required('ADMIN')
def admin_audit_log_view(request):
    """
    Comprehensive unified Admin audit surveillance workspace integrating 5 audit models:
    EmployeeActivityLog, ProfileAuditLog, LoanVerificationLog, DebitCardAuditLog, and TellerOperationLog.
    Supports source selection, search, filtering, pagination, and detail inspection.
    """
    source = request.GET.get('source', 'employee').strip().lower()
    query = request.GET.get('q', '').strip()
    selected_action = request.GET.get('action_type', '').strip().upper()
    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()

    items = []

    if source == 'profile':
        qs = ProfileAuditLog.objects.select_related('user', 'updated_by').all().order_by('-updated_at')
        if query:
            qs = qs.filter(
                Q(user__username__icontains=query) |
                Q(updated_by__username__icontains=query) |
                Q(changes__icontains=query)
            )
        if date_from:
            try:
                qs = qs.filter(updated_at__date__gte=date_from)
            except (ValueError, ValidationError):
                pass
        if date_to:
            try:
                qs = qs.filter(updated_at__date__lte=date_to)
            except (ValueError, ValidationError):
                pass
        for log in qs[:500]:
            items.append({
                'timestamp': log.updated_at,
                'actor_username': log.updated_by.username if log.updated_by else 'System',
                'actor_role': getattr(log.updated_by, 'role', 'SYSTEM'),
                'action': 'PROFILE_UPDATE',
                'target_display': f"Customer @{log.user.username}",
                'description': f"Customer profile updated by @{log.updated_by.username if log.updated_by else 'System'}",
                'changes': log.changes,
                'ip_address': log.ip_address,
            })

    elif source == 'loan':
        qs = LoanVerificationLog.objects.select_related('loan', 'loan__account', 'performed_by').all().order_by('-created_at')
        if query:
            qs = qs.filter(
                Q(loan__id__icontains=query.replace('#', '')) |
                Q(performed_by__username__icontains=query) |
                Q(notes__icontains=query) |
                Q(action__icontains=query)
            )
        if selected_action:
            qs = qs.filter(action__icontains=selected_action)
        if date_from:
            try:
                qs = qs.filter(created_at__date__gte=date_from)
            except (ValueError, ValidationError):
                pass
        if date_to:
            try:
                qs = qs.filter(created_at__date__lte=date_to)
            except (ValueError, ValidationError):
                pass
        for log in qs[:500]:
            acc_num = log.loan.account.account_number if log.loan and log.loan.account else ''
            items.append({
                'timestamp': log.created_at,
                'actor_username': log.performed_by.username if log.performed_by else 'System',
                'actor_role': getattr(log.performed_by, 'role', 'SYSTEM'),
                'action': log.action,
                'target_display': f"Loan #{log.loan_id}" + (f" (A/C ••••{acc_num[-4:]})" if acc_num else ""),
                'description': log.notes or log.get_action_display(),
                'changes': '',
                'ip_address': None,
            })

    elif source == 'card':
        qs = DebitCardAuditLog.objects.select_related('card', 'card__account', 'performed_by').all().order_by('-timestamp')
        if query:
            qs = qs.filter(
                Q(card__card_number__endswith=query[-4:]) |
                Q(performed_by__username__icontains=query) |
                Q(reason__icontains=query) |
                Q(action__icontains=query)
            )
        if selected_action:
            qs = qs.filter(action__icontains=selected_action)
        if date_from:
            try:
                qs = qs.filter(timestamp__date__gte=date_from)
            except (ValueError, ValidationError):
                pass
        if date_to:
            try:
                qs = qs.filter(timestamp__date__lte=date_to)
            except (ValueError, ValidationError):
                pass
        for log in qs[:500]:
            items.append({
                'timestamp': log.timestamp,
                'actor_username': log.performed_by.username if log.performed_by else 'System',
                'actor_role': getattr(log.performed_by, 'role', 'CUSTOMER'),
                'action': log.action,
                'target_display': f"Card {log.card.masked_card_number}",
                'description': log.reason or log.get_action_display(),
                'changes': '',
                'ip_address': None,
            })

    elif source == 'teller':
        qs = TellerOperationLog.objects.select_related('employee', 'customer', 'account').all().order_by('-created_at')
        if query:
            qs = qs.filter(
                Q(employee__username__icontains=query) |
                Q(customer_username_snapshot__icontains=query) |
                Q(account_number_snapshot__icontains=query) |
                Q(reference_id__icontains=query) |
                Q(description__icontains=query)
            )
        if selected_action:
            qs = qs.filter(operation_type__icontains=selected_action)
        if date_from:
            try:
                qs = qs.filter(created_at__date__gte=date_from)
            except (ValueError, ValidationError):
                pass
        if date_to:
            try:
                qs = qs.filter(created_at__date__lte=date_to)
            except (ValueError, ValidationError):
                pass
        for log in qs[:500]:
            amt_str = f"{log.amount:.2f}" if log.amount is not None else '0.00'
            items.append({
                'timestamp': log.created_at,
                'actor_username': log.employee.username if log.employee else 'Unknown',
                'actor_role': 'EMPLOYEE',
                'action': log.operation_type,
                'target_display': f"Ref #{log.reference_id} (A/C ••••{log.account_number_snapshot[-4:] if log.account_number_snapshot else ''})",
                'description': f"{log.get_operation_type_display()} of ₹{amt_str} [{log.status}]. {log.description}",
                'changes': f"Payload Hash: {log.payload_hash}" if log.payload_hash else '',
                'ip_address': None,
            })

    else:
        # Default: EmployeeActivityLog
        source = 'employee'
        qs = EmployeeActivityLog.objects.select_related('employee', 'customer').all().order_by('-created_at')
        if query:
            qs = qs.filter(
                Q(employee__username__icontains=query) |
                Q(customer__username__icontains=query) |
                Q(account_number__icontains=query) |
                Q(target_id__icontains=query) |
                Q(description__icontains=query)
            )
        if selected_action:
            qs = qs.filter(action_type__icontains=selected_action)
        if date_from:
            try:
                qs = qs.filter(created_at__date__gte=date_from)
            except (ValueError, ValidationError):
                pass
        if date_to:
            try:
                qs = qs.filter(created_at__date__lte=date_to)
            except (ValueError, ValidationError):
                pass
        for log in qs[:500]:
            target_str = ''
            if log.customer:
                target_str = f"Customer @{log.customer.username}"
            elif log.account_number:
                target_str = f"A/C ••••{log.account_number[-4:]}"
            elif log.target_id:
                target_str = f"Target #{log.target_id}"

            items.append({
                'timestamp': log.created_at,
                'actor_username': log.employee.username if log.employee else 'System',
                'actor_role': getattr(log.employee, 'role', 'SYSTEM'),
                'action': log.action_type,
                'target_display': target_str,
                'description': log.description,
                'changes': log.changes,
                'ip_address': log.ip_address,
            })

    counts = {
        'total_activity_logs': EmployeeActivityLog.objects.count(),
        'total_teller_logs': TellerOperationLog.objects.count(),
        'total_loan_logs': LoanVerificationLog.objects.count(),
        'total_card_logs': DebitCardAuditLog.objects.count(),
    }

    paginator = Paginator(items, 25)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    return render(request, 'admin_audit_log.html', {
        'page_obj': page_obj,
        'paginator': paginator,
        'counts': counts,
        'selected_source': source,
        'query': query,
        'selected_action': selected_action,
        'date_from': date_from or '',
        'date_to': date_to or '',
    })


@role_required('ADMIN')
def admin_audit_export_csv_view(request):
    """
    Exports filtered audit logs to CSV.
    Enforces maximum limit of 2,000 records, formula-injection protection,
    sensitive credential redaction, and audit logging.
    """
    source = request.GET.get('source', 'employee').strip().lower()
    query = request.GET.get('q', '').strip()
    selected_action = request.GET.get('action_type', '').strip().upper()
    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="bebank_audit_{source}_{timezone.now().strftime("%Y%m%d_%H%M%S")}.csv"'

    writer = csv.writer(response)
    writer.writerow([escape_csv_formula("BEBANK System Audit Log Export")])
    writer.writerow([escape_csv_formula(f"Audit Source: {source.upper()}")])
    writer.writerow([escape_csv_formula(f"Exported By: @{request.user.username}")])
    writer.writerow([escape_csv_formula(f"Exported At: {timezone.now().strftime('%Y-%m-%d %H:%M:%S')}")])
    writer.writerow([])

    writer.writerow([
        escape_csv_formula("Timestamp"),
        escape_csv_formula("Actor Username"),
        escape_csv_formula("Actor Role"),
        escape_csv_formula("Action Type"),
        escape_csv_formula("Target / Reference"),
        escape_csv_formula("Description / Justification"),
        escape_csv_formula("IP Address")
    ])

    if source == 'profile':
        qs = ProfileAuditLog.objects.select_related('user', 'updated_by').all().order_by('-updated_at')
        if query:
            qs = qs.filter(Q(user__username__icontains=query) | Q(updated_by__username__icontains=query) | Q(changes__icontains=query))
        if date_from:
            qs = qs.filter(updated_at__date__gte=date_from)
        if date_to:
            qs = qs.filter(updated_at__date__lte=date_to)
        rows = [
            [
                escape_csv_formula(log.updated_at.strftime('%Y-%m-%d %H:%M:%S')),
                escape_csv_formula(log.updated_by.username if log.updated_by else 'System'),
                escape_csv_formula(getattr(log.updated_by, 'role', 'SYSTEM')),
                escape_csv_formula('PROFILE_UPDATE'),
                escape_csv_formula(f"Customer @{log.user.username}"),
                escape_csv_formula(log.changes or "Profile modified"),
                escape_csv_formula(log.ip_address or "N/A")
            ]
            for log in qs[:2000]
        ]

    elif source == 'loan':
        qs = LoanVerificationLog.objects.select_related('loan', 'loan__account', 'performed_by').all().order_by('-created_at')
        if query:
            qs = qs.filter(Q(loan__id__icontains=query.replace('#', '')) | Q(performed_by__username__icontains=query) | Q(notes__icontains=query))
        if selected_action:
            qs = qs.filter(action__icontains=selected_action)
        if date_from:
            qs = qs.filter(created_at__date__gte=date_from)
        if date_to:
            qs = qs.filter(created_at__date__lte=date_to)
        rows = [
            [
                escape_csv_formula(log.created_at.strftime('%Y-%m-%d %H:%M:%S')),
                escape_csv_formula(log.performed_by.username if log.performed_by else 'System'),
                escape_csv_formula(getattr(log.performed_by, 'role', 'SYSTEM')),
                escape_csv_formula(log.action),
                escape_csv_formula(f"Loan #{log.loan_id}"),
                escape_csv_formula(log.notes or log.get_action_display()),
                escape_csv_formula("Not Captured")
            ]
            for log in qs[:2000]
        ]

    elif source == 'card':
        qs = DebitCardAuditLog.objects.select_related('card', 'card__account', 'performed_by').all().order_by('-timestamp')
        if query:
            qs = qs.filter(Q(card__card_number__endswith=query[-4:]) | Q(performed_by__username__icontains=query) | Q(reason__icontains=query))
        if selected_action:
            qs = qs.filter(action__icontains=selected_action)
        if date_from:
            qs = qs.filter(timestamp__date__gte=date_from)
        if date_to:
            qs = qs.filter(timestamp__date__lte=date_to)
        rows = [
            [
                escape_csv_formula(log.timestamp.strftime('%Y-%m-%d %H:%M:%S')),
                escape_csv_formula(log.performed_by.username if log.performed_by else 'System'),
                escape_csv_formula(getattr(log.performed_by, 'role', 'CUSTOMER')),
                escape_csv_formula(log.action),
                escape_csv_formula(f"Card {log.card.masked_card_number}"),
                escape_csv_formula(log.reason or log.get_action_display()),
                escape_csv_formula("Not Captured")
            ]
            for log in qs[:2000]
        ]

    elif source == 'teller':
        qs = TellerOperationLog.objects.select_related('employee').all().order_by('-created_at')
        if query:
            qs = qs.filter(Q(employee__username__icontains=query) | Q(reference_id__icontains=query) | Q(description__icontains=query))
        if selected_action:
            qs = qs.filter(operation_type__icontains=selected_action)
        if date_from:
            qs = qs.filter(created_at__date__gte=date_from)
        if date_to:
            qs = qs.filter(created_at__date__lte=date_to)
        rows = [
            [
                escape_csv_formula(log.created_at.strftime('%Y-%m-%d %H:%M:%S')),
                escape_csv_formula(log.employee.username if log.employee else 'Unknown'),
                escape_csv_formula('EMPLOYEE'),
                escape_csv_formula(log.operation_type),
                escape_csv_formula(f"Ref #{log.reference_id}"),
                escape_csv_formula(f"₹{log.amount or 0.00:.2f} [{log.status}]. {log.description}"),
                escape_csv_formula("Not Captured")
            ]
            for log in qs[:2000]
        ]

    else:
        qs = EmployeeActivityLog.objects.select_related('employee', 'customer').all().order_by('-created_at')
        if query:
            qs = qs.filter(Q(employee__username__icontains=query) | Q(customer__username__icontains=query) | Q(account_number__icontains=query) | Q(description__icontains=query))
        if selected_action:
            qs = qs.filter(action_type__icontains=selected_action)
        if date_from:
            qs = qs.filter(created_at__date__gte=date_from)
        if date_to:
            qs = qs.filter(created_at__date__lte=date_to)
        rows = [
            [
                escape_csv_formula(log.created_at.strftime('%Y-%m-%d %H:%M:%S')),
                escape_csv_formula(log.employee.username if log.employee else 'System'),
                escape_csv_formula(getattr(log.employee, 'role', 'SYSTEM')),
                escape_csv_formula(log.action_type),
                escape_csv_formula(f"A/C ••••{log.account_number[-4:]}" if log.account_number else (f"Customer @{log.customer.username}" if log.customer else (f"Ref #{log.target_id}" if log.target_id else 'N/A'))),
                escape_csv_formula(log.description),
                escape_csv_formula(log.ip_address or "N/A")
            ]
            for log in qs[:2000]
        ]

    for r in rows:
        writer.writerow(r)

    log_employee_activity(
        employee=request.user,
        action_type='REPORT_EXPORT',
        description=f"Admin exported audit trail CSV ({source.upper()}) - {len(rows)} rows",
        request=request
    )

    return response


# =========================
# BLOCK USER
# =========================
@role_required('ADMIN')
@require_POST
def block_user(request, user_id):

    user = get_object_or_404(User, id=user_id)

    # 🔴 SELF-LOCKOUT GUARD
    if request.user.id == user.id:
        raise PermissionDenied("Administrators cannot block their own account.")

    # 🔴 SUPERUSER PROTECTION
    if getattr(user, 'is_superuser', False) and not getattr(request.user, 'is_superuser', False):
        raise PermissionDenied("Non-superuser administrators cannot block a superuser account.")

    user.account_locked = True
    user.save(update_fields=['account_locked'])

    messages.success(request, "User blocked 🚫")

    return redirect('/admin-dashboard/')


# =========================
# UNBLOCK USER
# =========================
@role_required('ADMIN')
@require_POST
def unblock_user(request, user_id):

    user = get_object_or_404(User, id=user_id)

    # 🔴 SUPERUSER PROTECTION
    if getattr(user, 'is_superuser', False) and not getattr(request.user, 'is_superuser', False):
        raise PermissionDenied("Non-superuser administrators cannot unblock a superuser account.")

    user.account_locked = False
    user.save(update_fields=['account_locked'])

    messages.success(request, "User unblocked ✅")

    return redirect('/admin-dashboard/')


# =========================
# LOGOUT
# =========================
def logout_view(request):
    logout(request)
    return redirect('/login/')
