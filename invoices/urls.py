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

    path('territories/', views.territory_list, name='territory_list'),
    path('territories/new/', views.territory_edit, name='territory_new'),
    path(
        'territories/<int:territory_id>/edit/',
        views.territory_edit,
        name='territory_edit',
    ),
    path('territories/report/', views.territory_report, name='territory_report'),

    path('team/', views.team_list, name='team_list'),
    path('team/new/', views.employee_edit, name='employee_new'),
    path(
        'team/<int:employee_id>/edit/',
        views.employee_edit,
        name='employee_edit',
    ),

    path('call-points/', views.call_point_list, name='call_point_list'),
    path('call-points/new/', views.call_point_edit, name='call_point_new'),
    path(
        'call-points/<int:call_point_id>/edit/',
        views.call_point_edit,
        name='call_point_edit',
    ),

    path('plans/', views.plan_list, name='plan_list'),
    path('plans/generate/', views.plan_generate, name='plan_generate'),
    path('plans/<int:plan_id>/', views.plan_detail, name='plan_detail'),
    path(
        'plans/<int:plan_id>/<str:action>/',
        views.plan_status,
        name='plan_status',
    ),
    path(
        'visits/<int:visit_id>/<str:action>/',
        views.visit_status,
        name='visit_status',
    ),
]
