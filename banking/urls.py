from django.urls import path
from . import views

urlpatterns = [

    # =========================
    # ACCOUNT
    # =========================
    path('create-account/', views.create_account_view, name="create_account"),

    # =========================
    # TRANSACTIONS & TELLER COUNTER
    # =========================
    path('deposit/', views.deposit_view, name='deposit'),
    path('withdraw/', views.withdraw_view, name="withdraw"),
    path('transfer/', views.transfer_view, name="transfer"),
    path('staff/deposit/', views.staff_deposit_view, name="staff_deposit"),
    path('staff/withdraw/', views.staff_withdraw_view, name="staff_withdraw"),
    path('staff/operations/', views.staff_operations_view, name="staff_operations"),
    path('staff/teller/history/', views.staff_teller_history_view, name="staff_teller_history"),
    path('staff/teller/receipt/<str:reference_id>/', views.staff_teller_receipt_view, name="staff_teller_receipt"),
    path('staff/teller/export/', views.staff_teller_export_csv_view, name="staff_teller_export_csv"),

    # =========================
    # BENEFICIARIES
    # =========================
    path('beneficiaries/', views.beneficiary_list_view, name="beneficiary_list"),
    path('beneficiaries/<int:beneficiary_id>/edit/', views.beneficiary_edit_view, name="beneficiary_edit"),
    path('beneficiaries/<int:beneficiary_id>/delete/', views.beneficiary_delete_view, name="beneficiary_delete"),

    # =========================
    # HISTORY + PDF
    # =========================
    path('history/', views.history_view, name="history"),
    path('history/<int:account_id>/', views.history_view, name="history_detail"),
    path('history/pdf/', views.passbook_pdf_view, name="history_pdf"),
    path('history/pdf/<int:account_id>/', views.passbook_pdf_view, name="history_pdf_detail"),

    # ✅ OPTIONAL (no more 404)
    path('passbook/', views.passbook_pdf_view, name="passbook"),
    path('passbook/<int:account_id>/', views.passbook_pdf_view, name="passbook_detail"),

    # =========================
    # BANK SERVICES
    # =========================
    path('fd/', views.fd_view, name="fd"),
    path('fd/<int:fd_id>/', views.fd_detail_view, name="fd_detail"),
    path('fd/<int:fd_id>/settle/', views.fd_settle_view, name="fd_settle"),
    path('fd/<int:fd_id>/close/', views.fd_close_view, name="fd_close"),
    path('loan/', views.loan_view, name="loan"),
    path('loan/<int:loan_id>/', views.loan_detail_view, name="loan_detail"),
    path('loan/<int:loan_id>/repay/', views.loan_repay_view, name="loan_repay"),
    path('loan/<int:loan_id>/approve/', views.approve_loan, name='approve_loan'),
    path('loan/<int:loan_id>/reject/', views.reject_loan, name='reject_loan'),

    # STAFF FINANCIAL PRODUCT OPERATIONS (TASK 3.3)
    path('staff/loans/', views.staff_loan_queue_view, name="staff_loan_queue"),
    path('staff/loans/<int:loan_id>/', views.staff_loan_detail_view, name="staff_loan_detail"),
    path('staff/loans/<int:loan_id>/verify/', views.staff_loan_verify_action_view, name="staff_loan_verify_action"),
    path('staff/fixed-deposits/', views.staff_fd_list_view, name="staff_fd_list"),
    path('staff/fixed-deposits/<int:fd_id>/', views.staff_fd_detail_view, name="staff_fd_detail"),
    path('staff/fixed-deposits/export/', views.staff_fd_export_csv_view, name="staff_fd_export_csv"),

    # =========================
    # VIRTUAL DEBIT CARDS
    # =========================
    path('cards/', views.card_list_view, name="card_list"),
    path('cards/issue/', views.card_issue_view, name="card_issue"),
    path('cards/<int:card_id>/', views.card_detail_view, name="card_detail"),
    path('cards/<int:card_id>/block/', views.card_block_view, name="card_block"),
    path('cards/<int:card_id>/unblock/', views.card_unblock_view, name="card_unblock"),
    path('cards/<int:card_id>/cancel/', views.card_cancel_view, name="card_cancel"),
    path('staff/cards/', views.staff_card_list_view, name="staff_card_list"),
    path('staff/cards/<int:card_id>/', views.staff_card_detail_view, name="staff_card_detail"),
    path('staff/cards/<int:card_id>/admin-action/', views.admin_card_action_view, name="admin_card_action"),

    # =========================
    # IN-APP NOTIFICATIONS
    # =========================
    path('notifications/', views.notification_list_view, name="notifications_list"),
    path('notifications/<int:notification_id>/read/', views.notification_mark_read_view, name="notification_mark_read"),
    path('notifications/mark-all-read/', views.notification_mark_all_read_view, name="notification_mark_all_read"),

    # =========================
    # CUSTOMER SUPPORT & TICKETS
    # =========================
    path('support/', views.support_ticket_list_view, name="support_ticket_list"),
    path('support/new/', views.support_ticket_create_view, name="support_ticket_create"),
    path('support/<int:ticket_id>/', views.support_ticket_detail_view, name="support_ticket_detail"),
    path('support/<int:ticket_id>/reply/', views.support_ticket_reply_view, name="support_ticket_reply"),
    path('support/<int:ticket_id>/reopen/', views.support_ticket_reopen_view, name="support_ticket_reopen"),

    # =========================
    # STAFF SUPPORT QUEUE & RESOLUTION
    # =========================
    path('staff/support/', views.staff_support_queue_view, name="staff_support_queue"),
    path('staff/support/<int:ticket_id>/', views.staff_support_detail_view, name="staff_support_detail"),
    path('staff/support/<int:ticket_id>/reply/', views.staff_support_reply_view, name="staff_support_reply"),
    path('staff/support/<int:ticket_id>/assign/', views.staff_support_assign_view, name="staff_support_assign"),
    path('admin-support-assign/<int:ticket_id>/', views.admin_support_assign_view, name="admin_support_assign"),

    # =========================
    # EMPLOYEE REPORTS & ANALYTICS (TASK 3.4)
    # =========================
    path('analytics/', views.analytics_view, name="analytics"),
    path('staff/reports/', views.analytics_view, name="staff_reports"),
    path('staff/reports/export/', views.staff_reports_export_csv_view, name="staff_reports_export_csv"),

    # =========================
    # EMPLOYEE ACTIVITY & AUDIT LOGS (TASK 3.4)
    # =========================
    path('staff/activity/', views.staff_activity_log_view, name="staff_activity_log"),
    path('staff/activity/export/', views.staff_activity_export_csv_view, name="staff_activity_export_csv"),
]
