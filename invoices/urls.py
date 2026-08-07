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

    path('daily/', views.daily_calls, name='daily_calls'),
    path('calls/', views.call_report_list, name='call_report_list'),
    path('calls/new/', views.call_report_create, name='call_report_new'),
    path('calls/summary/', views.call_report_summary, name='call_report_summary'),
    path(
        'calls/visit/<int:visit_id>/',
        views.call_report_create,
        name='call_report_for_visit',
    ),

    path('distributors/', views.distributor_list, name='distributor_list'),
    path('distributors/new/', views.distributor_edit, name='distributor_new'),
    path(
        'distributors/<int:distributor_id>/edit/',
        views.distributor_edit,
        name='distributor_edit',
    ),
    path(
        'distributors/<int:distributor_id>/layout/',
        views.distributor_layout,
        name='distributor_layout',
    ),
    path(
        'distributors/<int:distributor_id>/detect/',
        views.distributor_detect,
        name='distributor_detect',
    ),
    path(
        'distributors/<int:distributor_id>/preview/',
        views.distributor_preview,
        name='distributor_preview',
    ),

    path('products/', views.product_list, name='product_list'),
    path('products/new/', views.product_edit, name='product_new'),
    path(
        'products/<int:product_id>/edit/',
        views.product_edit,
        name='product_edit',
    ),
    path(
        'products/<int:product_id>/batches/',
        views.product_batches,
        name='product_batches',
    ),

    path('manufacturers/', views.manufacturer_list, name='manufacturer_list'),
    path('manufacturers/new/', views.manufacturer_edit, name='manufacturer_new'),
    path(
        'manufacturers/<int:manufacturer_id>/',
        views.manufacturer_detail,
        name='manufacturer_detail',
    ),
    path(
        'manufacturers/<int:manufacturer_id>/edit/',
        views.manufacturer_edit,
        name='manufacturer_edit',
    ),

    path('suppliers/', views.supplier_list, name='supplier_list'),
    path('suppliers/new/', views.supplier_edit, name='supplier_new'),
    path(
        'suppliers/<int:supplier_id>/edit/',
        views.supplier_edit,
        name='supplier_edit',
    ),

    path('purchases/', views.purchase_list, name='purchase_list'),
    path('purchases/new/', views.purchase_create, name='purchase_new'),
    path(
        'purchases/<int:purchase_id>/edit/',
        views.purchase_edit,
        name='purchase_edit',
    ),

    path('returns/', views.return_list, name='return_list'),
    path(
        'returns/invoice/<int:invoice_id>/',
        views.return_create,
        name='return_create',
    ),

    path('expenses/', views.expense_list, name='expense_list'),
    path('expenses/new/', views.expense_edit, name='expense_new'),
    path('expenses/report/', views.expense_report, name='expense_report'),
    path(
        'expenses/<int:expense_id>/edit/',
        views.expense_edit,
        name='expense_edit',
    ),
    path(
        'expenses/<int:expense_id>/<str:action>/',
        views.expense_status,
        name='expense_status',
    ),
    path(
        'expense-categories/',
        views.expense_category_list,
        name='expense_category_list',
    ),
    path(
        'expense-categories/new/',
        views.expense_category_edit,
        name='expense_category_new',
    ),
    path(
        'expense-categories/<int:category_id>/edit/',
        views.expense_category_edit,
        name='expense_category_edit',
    ),

    path('samples/', views.sample_list, name='sample_list'),
    path('samples/new/', views.sample_create, name='sample_new'),
    path('samples/report/', views.sample_report, name='sample_report'),

    path('payroll/', views.payroll_list, name='payroll_list'),
    path('payroll/create/', views.payroll_create, name='payroll_create'),
    path('payroll/<int:run_id>/', views.payroll_detail, name='payroll_detail'),
    path(
        'payroll/<int:run_id>/finalise/',
        views.payroll_finalise,
        name='payroll_finalise',
    ),
    path(
        'payslips/<int:payslip_id>/edit/',
        views.payslip_edit,
        name='payslip_edit',
    ),
    path('payslips/<int:payslip_id>/pdf/', views.payslip_pdf, name='payslip_pdf'),

    path('stock/', views.stock_report, name='stock_report'),
    path('stock/movements/', views.stock_movements, name='stock_movements'),
    path('stock/ledger/', views.stock_ledger, name='stock_ledger'),
    path(
        'stock/batches/<int:batch_id>/adjust/',
        views.batch_adjust,
        name='batch_adjust',
    ),
]
