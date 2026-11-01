from django.urls import path
from . import views

urlpatterns = [

    path('', views.login_view, name='login'),   # root login

    path('login/', views.login_view, name='login'),

    path('customer/dashboard/', views.customer_dashboard, name='customer_dashboard'),
    path('customer/dashboard/<int:account_id>/', views.customer_dashboard, name='customer_dashboard_detail'),

    path('employee/dashboard/', views.employee_dashboard, name='employee_dashboard'),

    path('admin-dashboard/', views.admin_dashboard, name='admin_dashboard'),

    path('users/<int:user_id>/block/', views.block_user, name='block_user'),
    path('users/<int:user_id>/unblock/', views.unblock_user, name='unblock_user'),

    path('logout/', views.logout_view, name='logout'),
]
