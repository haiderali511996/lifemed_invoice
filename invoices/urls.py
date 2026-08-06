from django.urls import path
from . import views

urlpatterns = [
    path('', views.index, name='index'),
    path('dashboard/', views.dashboard, name='dashboard'),
    path('generate/', views.generate_invoice, name='generate'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('logs/', views.invoice_logs_view, name='invoice_logs'),

    path('search/', views.global_search, name='search'),
    path('profile/', views.profile, name='profile'),

    path('customers/', views.customer_list, name='customer_list'),
    path(
        'customers/<int:customer_id>/edit/',
        views.customer_edit,
        name='customer_edit',
    ),
    path(
        'customers/<int:customer_id>/last-invoice/',
        views.customer_last_invoice,
        name='customer_last_invoice',
    ),

    path('ledgers/', views.ledger_list, name='ledger_list'),
    path(
        'ledgers/<int:customer_id>/',
        views.customer_ledger,
        name='customer_ledger',
    ),
    path(
        'ledgers/<int:customer_id>/payment/',
        views.payment_create,
        name='payment_create',
    ),
    path('payments/', views.payment_list, name='payment_list'),
]
