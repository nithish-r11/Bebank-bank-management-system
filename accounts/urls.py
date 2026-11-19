from django.urls import path
from django.views.generic.base import RedirectView
from . import views

urlpatterns = [

    path('', RedirectView.as_view(url='/login/', permanent=False), name='root_redirect'),
    path('login/', views.login_view, name='login'),
    path('terms/', views.terms_view, name='terms'),

    path('customer/dashboard/', views.customer_dashboard, name='customer_dashboard'),
    path('customer/dashboard/<int:account_id>/', views.customer_dashboard, name='customer_dashboard_detail'),
    path('customer/profile/', views.customer_profile_view, name='customer_profile'),
    path('customer/kyc/', views.customer_kyc_view, name='customer_kyc'),

    path('staff/kyc/', views.staff_kyc_queue_view, name='staff_kyc_queue'),
    path('staff/kyc/<int:user_id>/review/', views.staff_kyc_review_view, name='staff_kyc_review'),
    path('staff/customers/', views.staff_customer_list_view, name='staff_customer_list'),
    path('staff/customers/<int:user_id>/', views.staff_customer_detail_view, name='staff_customer_detail'),
    path('staff/customers/<int:user_id>/profile/edit/', views.staff_customer_profile_edit_view, name='staff_customer_profile_edit'),
    path('staff/operations/', views.staff_operations_view, name='staff_operations'),
    path('staff/create-account/', views.create_account_view, name='staff_create_account'),
    path('staff/deposit/', views.staff_deposit_view, name='staff_deposit'),
    path('staff/withdraw/', views.staff_withdraw_view, name='staff_withdraw'),
    path('staff/teller/history/', views.staff_teller_history_view, name='staff_teller_history'),
    path('staff/teller/receipt/<str:reference_id>/', views.staff_teller_receipt_view, name='staff_teller_receipt'),
    path('staff/teller/export/', views.staff_teller_export_csv_view, name='staff_teller_export_csv'),
    path('staff/loans/', views.staff_loan_queue_view, name='staff_loan_queue'),
    path('staff/loans/<int:loan_id>/', views.staff_loan_detail_view, name='staff_loan_detail'),
    path('staff/loans/<int:loan_id>/verify/', views.staff_loan_verify_action_view, name='staff_loan_verify_action'),
    path('staff/fixed-deposits/', views.staff_fd_list_view, name='staff_fd_list'),
    path('staff/fixed-deposits/<int:fd_id>/', views.staff_fd_detail_view, name='staff_fd_detail'),
    path('staff/fixed-deposits/export/', views.staff_fd_export_csv_view, name='staff_fd_export_csv'),
    path('staff/cards/', views.staff_card_list_view, name='staff_card_list'),
    path('staff/cards/<int:card_id>/', views.staff_card_detail_view, name='staff_card_detail'),
    path('staff/cards/<int:card_id>/admin-action/', views.admin_card_action_view, name='admin_card_action'),
    path('staff/support/', views.staff_support_queue_view, name='staff_support_queue'),
    path('staff/support/<int:ticket_id>/', views.staff_support_detail_view, name='staff_support_detail'),
    path('staff/support/<int:ticket_id>/reply/', views.staff_support_reply_view, name='staff_support_reply'),
    path('staff/support/<int:ticket_id>/assign/', views.staff_support_assign_view, name='staff_support_assign'),
    path('staff/reports/', views.analytics_view, name='staff_reports'),
    path('staff/reports/export/', views.staff_reports_export_csv_view, name='staff_reports_export_csv'),
    path('staff/activity/', views.staff_activity_log_view, name='staff_activity_log'),
    path('staff/activity/export/', views.staff_activity_export_csv_view, name='staff_activity_export_csv'),
    path('staff/profile/', views.staff_profile_view, name='staff_profile'),

    path('employee/dashboard/', views.employee_dashboard, name='employee_dashboard'),

    # =========================
    # ADMIN CONTROL CENTER & EMPLOYEE MANAGEMENT (TASK 4.1)
    # =========================
    path('admin-dashboard/', views.admin_dashboard, name='admin_dashboard'),
    path('admin-portal/employees/', views.admin_employee_list_view, name='admin_employee_list'),
    path('admin-portal/employees/new/', views.admin_employee_create_view, name='admin_employee_create'),
    path('admin-portal/employees/<int:user_id>/', views.admin_employee_detail_view, name='admin_employee_detail'),
    path('admin-portal/employees/<int:user_id>/edit/', views.admin_employee_edit_view, name='admin_employee_edit'),
    path('admin-portal/employees/<int:user_id>/toggle-active/', views.admin_employee_toggle_active_view, name='admin_employee_toggle_active'),
    path('admin-portal/employees/<int:user_id>/toggle-lock/', views.admin_employee_toggle_lock_view, name='admin_employee_toggle_lock'),
    path('admin-portal/permissions/', views.admin_permissions_view, name='admin_permissions'),
    path('admin-portal/settings/', views.admin_settings_view, name='admin_settings'),
    path('admin-portal/backup/', views.admin_backup_view, name='admin_backup'),
    path('admin-portal/backup/create/', views.admin_backup_create_view, name='admin_backup_create'),
    path('admin-portal/backup/download/', views.admin_backup_download_view, name='admin_backup_download'),

    # =========================
    # ADMIN CUSTOMER & ACCOUNT GOVERNANCE (TASK 4.2)
    # =========================
    path('admin-portal/customers/', views.admin_customer_list_view, name='admin_customer_list'),
    path('admin-portal/customers/<int:user_id>/', views.admin_customer_detail_view, name='admin_customer_detail'),
    path('admin-portal/customers/<int:user_id>/toggle-active/', views.admin_customer_toggle_active_view, name='admin_customer_toggle_active'),
    path('admin-portal/customers/<int:user_id>/toggle-lock/', views.admin_customer_toggle_lock_view, name='admin_customer_toggle_lock'),
    path('admin-portal/accounts/', views.admin_account_list_view, name='admin_account_list'),
    path('admin-portal/accounts/<int:account_id>/', views.admin_account_detail_view, name='admin_account_detail'),
    path('admin-portal/accounts/<int:account_id>/toggle-freeze/', views.admin_account_toggle_freeze_view, name='admin_account_toggle_freeze'),

    # =========================
    # ADMIN TRANSACTION MONITORING & FINANCIAL CONTROLS (TASK 4.3)
    # =========================
    path('admin-portal/transactions/', views.admin_transaction_list_view, name='admin_transaction_list'),
    path('admin-portal/transactions/export/', views.admin_transaction_export_csv_view, name='admin_transaction_export_csv'),
    path('admin-portal/transactions/<int:transaction_id>/', views.admin_transaction_detail_view, name='admin_transaction_detail'),
    path('admin-portal/reconciliation/', views.admin_reconciliation_view, name='admin_reconciliation'),
    path('admin-portal/reconciliation/export/', views.admin_reconciliation_export_csv_view, name='admin_reconciliation_export_csv'),
    path('admin-portal/risk-monitoring/', views.admin_risk_monitoring_view, name='admin_risk_monitoring'),

    # =========================
    # ADMIN PRODUCT GOVERNANCE & NOTIFICATIONS (TASK 4.4)
    # =========================
    path('admin-portal/loans/', views.admin_loan_list_view, name='admin_loan_list'),
    path('admin-portal/loans/<int:loan_id>/', views.admin_loan_detail_view, name='admin_loan_detail'),
    path('admin-portal/loans/<int:loan_id>/approve/', views.admin_loan_approve_view, name='admin_loan_approve'),
    path('admin-portal/loans/<int:loan_id>/reject/', views.admin_loan_reject_view, name='admin_loan_reject'),

    path('admin-portal/fixed-deposits/', views.admin_fd_list_view, name='admin_fd_list'),
    path('admin-portal/fixed-deposits/<int:fd_id>/', views.admin_fd_detail_view, name='admin_fd_detail'),
    path('admin-portal/fixed-deposits/<int:fd_id>/settle/', views.admin_fd_settle_view, name='admin_fd_settle'),
    path('admin-portal/fixed-deposits/<int:fd_id>/close/', views.admin_fd_close_view, name='admin_fd_close'),

    path('admin-portal/cards/', views.admin_card_list_view, name='admin_card_list'),
    path('admin-portal/cards/<int:card_id>/', views.admin_card_detail_view, name='admin_card_detail'),

    path('admin-portal/notifications/', views.admin_notification_list_view, name='admin_notification_list'),
    path('admin-portal/notifications/broadcast/', views.admin_notification_broadcast_view, name='admin_notification_broadcast'),
    path('admin-portal/notifications/preview/', views.admin_notification_preview_view, name='admin_notification_preview'),
    path('admin-portal/notifications/<int:notif_id>/', views.admin_notification_detail_view, name='admin_notification_detail'),

    # =========================
    # ADMIN REPORTS, AUDIT & SYSTEM CONTROLS (TASK 4.5)
    # =========================
    path('admin-portal/reports/', views.admin_reports_view, name='admin_reports'),
    path('admin-portal/reports/export/', views.admin_reports_export_csv_view, name='admin_reports_export_csv'),
    path('admin-portal/audit/', views.admin_audit_log_view, name='admin_audit_log'),
    path('admin-portal/audit/export/', views.admin_audit_export_csv_view, name='admin_audit_export_csv'),

    path('users/<int:user_id>/block/', views.block_user, name='block_user'),
    path('users/<int:user_id>/unblock/', views.unblock_user, name='unblock_user'),

    path('logout/', views.logout_view, name='logout'),
]
