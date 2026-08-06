from django.urls import path
from . import views

urlpatterns = [
    path('', views.index, name='index'),
    path('generate/', views.generate_invoice, name='generate'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('logs/', views.invoice_logs_view, name='invoice_logs'),
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
]