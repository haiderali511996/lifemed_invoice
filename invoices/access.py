"""What a field login is allowed to reach.

An MR sees their own work: their day, their schedule, their doctors, their
samples, their expense claims, their sales and their payslips. Everything else
- purchasing, stock adjustments, other people's ledgers, payroll runs, company
reports - belongs to the office.

The gate is an allowlist rather than a blocklist, and it is enforced here in
one place rather than by a decorator on each view. A view added later is
closed to field staff until somebody adds it to this list on purpose, which is
the right way round: a missing entry is an inconvenience, a missing decorator
is a leak.
"""

from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse

from .models import is_field_staff

# URL names an MR login may open. Views that serve everyone filter their own
# querysets down to the signed-in employee - see field_employee() in models.
FIELD_ALLOWED = frozenset({
    # The portal
    "my_dashboard",
    "my_plan",
    "my_sales",
    "my_payslips",

    # Their working day
    "daily_calls",
    "call_report_new",
    "call_report_for_visit",
    "call_report_list",
    "visit_status",
    "plan_list",
    "plan_detail",
    "call_point_list",

    # Samples they hand out, scoped to them
    "sample_list",
    "sample_new",
    "product_batches",

    # Their own expense claims
    "expense_list",
    "expense_new",
    "expense_edit",

    # Their payslips
    "payslip_pdf",

    # Account
    "profile",
    "login",
    "logout",
})

# Everything under this URL namespace is exempt: see process_view.
API_NAMESPACE = "api"


class FieldStaffMiddleware:
    """Keep an MR login inside the portal."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        if not is_field_staff(request.user):
            return None

        match = request.resolver_match

        # The mobile API does its own scoping and must answer in JSON. Bouncing
        # it to an HTML dashboard would leave the app parsing a login page.
        if getattr(match, "namespace", "") == API_NAMESPACE:
            return None

        name = getattr(match, "url_name", None)

        if name is None or name in FIELD_ALLOWED:
            return None

        # Django's admin has its own permission checks, but an MR has no
        # business there either, so it never gets that far.
        messages.error(
            request,
            "🚫 That part of the system is for the office. "
            "Here is your own work instead.",
        )

        return redirect(reverse("my_dashboard"))
