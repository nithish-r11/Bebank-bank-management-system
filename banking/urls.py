from django.urls import path
from . import views

urlpatterns = [

    # =========================
    # ACCOUNT
    # =========================
    path('create-account/', views.create_account_view, name="create_account"),

    # =========================
    # TRANSACTIONS
    # =========================
    path('deposit/', views.deposit_view, name='deposit'),
    path('withdraw/', views.withdraw_view, name="withdraw"),
    path('transfer/', views.transfer_view, name="transfer"),

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
    path('fd/<int:fd_id>/', views.fd_view, name="fd_detail"),
    path('loan/', views.loan_view, name="loan"),
    path('loan/<int:loan_id>/', views.loan_view, name="loan_detail"),

    # =========================
    # ANALYTICS
    # =========================
    path('analytics/', views.analytics_view, name="analytics"),
]
