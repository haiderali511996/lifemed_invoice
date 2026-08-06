from django.shortcuts import render, redirect, get_object_or_404
from django.http import FileResponse, JsonResponse
from django.urls import reverse
from django.contrib.auth.decorators import login_required
from django.contrib.auth import authenticate, login, logout
from django.utils.timezone import now
from django.contrib import messages

from django.db.models import Count, DecimalField, F, OuterRef, Q, Subquery, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from datetime import timedelta

from .forms import (
    CallPointForm,
    CustomerForm,
    EmployeeForm,
    PaymentForm,
    PlanGenerateForm,
    ProfileForm,
    TerritoryForm,
)
from .planning import current_week_start, generate_plan
from .models import (
    CallPoint,
    Customer,
    Employee,
    Invoice,
    Item,
    InvoiceLog,
    Payment,
    PlanVisit,
    Territory,
    UserRolls,
    WeeklyPlan,
    ZERO,
    OVERDUE_DAYS,
    is_super_admin,
)

try:
    # PyMuPDF renamed its module to `pymupdf`; `fitz` is the pre-1.24.3 name.
    import pymupdf as fitz
except ImportError:
    import fitz

import os
import io
from decimal import Decimal

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PDF_PATH = os.path.join(BASE_DIR, "template.pdf")


def safe_decimal(value, default="0.00"):
    try:
        value = str(value).strip()

        if value == "":
            return Decimal(default)

        return Decimal(value)

    except Exception:
        return Decimal(default)


def post_column(request, field, length):
    """Read one item column, padded to `length` so a short list can't IndexError.

    Browsers omit nothing here, but a hand-built or partially filled POST can
    send fewer batch/expiry values than item names.
    """
    values = request.POST.getlist(field)

    return values + [""] * (length - len(values))


def wipe_rect(page, rect):
    r = fitz.Rect(rect)
    page.add_redact_annot(r, fill=(1, 1, 1))


def write_in_rect(page, rect, text, fontsize=9):
    r = fitz.Rect(rect)

    page.insert_text(
        (r.x0 + 2, r.y1 - 2),
        str(text),
        fontsize=fontsize
    )


def write_in_rect_right(page, rect, text, fontsize=9):
    r = fitz.Rect(rect)

    text = str(text)
    text_width = fitz.get_text_length(text, fontsize=fontsize)

    x = r.x1 - text_width - 2
    y = r.y1 - 3

    page.insert_text((x, y), text, fontsize=fontsize)


def login_view(request):

    if request.method == "POST":

        username_input = request.POST.get("username")
        password_input = request.POST.get("password")

        user = authenticate(
            request,
            username=username_input,
            password=password_input
        )

        if user is not None:

            login(request, user)

            # SUPER ADMIN → LOGS PAGE, EVERYONE ELSE → INVOICE FORM
            if is_super_admin(user):
                return redirect("invoice_logs")

            return redirect("index")

        return render(
            request,
            "invoices/login.html",
            {
                "error": "Invalid credentials"
            }
        )

    return render(request, "invoices/login.html")

def logout_view(request):
    logout(request)
    return redirect("login")


@login_required
def index(request):

    # SUPER ADMIN KO FORM NA DIKHAYE
    if is_super_admin(request.user):
        return redirect("invoice_logs")

    customers = Customer.objects.all()

    return render(
        request,
        "invoices/index.html",
        {
            "customers": customers
        }
    )


@login_required
def customer_list(request):
    query = request.GET.get("q", "").strip()

    customers = Customer.objects.annotate(invoice_count=Count("invoice"))

    if query:
        customers = customers.filter(
            Q(name__icontains=query)
            | Q(contact_person__icontains=query)
            | Q(contact_number__icontains=query)
        )

    return render(
        request,
        "invoices/customers.html",
        {
            "customers": customers,
            "query": query,
        }
    )


@login_required
def customer_edit(request, customer_id):
    customer = get_object_or_404(Customer, pk=customer_id)

    if request.method == "POST":
        form = CustomerForm(request.POST, instance=customer)

        if form.is_valid():
            form.save()

            messages.success(request, f"Saved {customer.name}.")

            return redirect("customer_list")

    else:
        form = CustomerForm(instance=customer)

    return render(
        request,
        "invoices/customer_form.html",
        {
            "form": form,
            "customer": customer,
        }
    )


@login_required
def customer_last_invoice(request, customer_id):
    """Customer details plus the line items of their most recent invoice.

    Used by the form to prefill a repeat order instead of retyping every row.
    """
    customer = get_object_or_404(Customer, pk=customer_id)

    invoice = Invoice.objects.filter(customer=customer).order_by("-id").first()

    items = []

    if invoice is not None:
        items = [
            {
                "name": item.name,
                "qty": item.qty,
                "price": f"{item.price:.2f}",
                "discount": f"{item.discount:.2f}",
                "batch": item.batch or "",
                "expiry": item.expiry or "",
            }
            for item in invoice.items.all()
        ]

    overdue = customer.overdue_invoices()

    return JsonResponse({
        "customer_name": customer.name,
        "address": customer.address or "",
        "ntn": customer.ntn or "",
        "sales_tax": customer.sales_tax or "",
        "license_no": customer.license_no or "",
        "contact_person": customer.contact_person or "",
        "contact_number": customer.contact_number or "",
        "last_invoice_no": invoice.invoice_no if invoice else None,
        "items": items,

        # Shown on the form so the operator sees what is already owed before
        # adding another invoice to the pile.
        "previous_balance": f"{customer.outstanding_balance:.2f}",
        "overdue_count": len(overdue),
        "overdue_amount": f"{sum((i.balance for i in overdue), ZERO):.2f}",
        "ledger_url": reverse("customer_ledger", args=[customer.pk]),
    })


@login_required
def generate_invoice(request):

    # SUPER ADMIN BLOCK
    if is_super_admin(request.user):
        return redirect("invoice_logs")

    if request.method == "POST":

        license_no = request.POST.get("license_no", "")

        customer_fields = {
            "address": request.POST.get("address", ""),
            "ntn": request.POST.get("ntn", ""),
            "sales_tax": request.POST.get("sales_tax", ""),
            "license_no": license_no,
        }

        customer, created = Customer.objects.get_or_create(
            name=request.POST.get("customer_name"),
            defaults=customer_fields
        )

        if not created:
            for field, value in customer_fields.items():
                setattr(customer, field, value)

            customer.save()

        invoice = Invoice.objects.create(
            customer=customer,
            license_no=license_no
        )

        names = request.POST.getlist("item_name[]")
        row_count = len(names)

        qtys = post_column(request, "qty[]", row_count)
        prices = post_column(request, "price[]", row_count)
        discounts = post_column(request, "discount[]", row_count)
        batches = post_column(request, "batch[]", row_count)
        expiries = post_column(request, "expiry[]", row_count)

        total_gross = Decimal("0")
        total_net = Decimal("0")
        total_discount = Decimal("0")

        # ITEMS LOOP
        for i in range(len(names)):

            if not names[i]:
                continue

            qty = safe_decimal(qtys[i])
            price = safe_decimal(prices[i])
            disc = safe_decimal(discounts[i])

            gross = Decimal(price) * Decimal(qty)

            discount_amount = (
                Decimal(price) *
                Decimal(disc) /
                Decimal("100")
            ) * Decimal(qty)

            total_gross += gross
            total_discount += discount_amount

            discounted_price = (
                Decimal(price) -
                (
                    Decimal(price) *
                    Decimal(disc) /
                    Decimal("100")
                )
            )

            amount = discounted_price * Decimal(qty)

            total_net += amount

            Item.objects.create(
                invoice=invoice,
                name=names[i],
                qty=int(float(qty)),
                batch=batches[i],
                expiry=expiries[i],
                price=price,
                discount=disc
            )

        # Store the net payable so ledgers never recompute it from line items
        invoice.total = total_net
        invoice.save(update_fields=["total"])

        # LOG ENTRY
        InvoiceLog.objects.create(
            invoice=invoice,
            user=request.user,
            customer_name=customer.name,
            amount=total_net,
            action="Invoice Created"
        )

        doc = fitz.open(PDF_PATH)
        page = doc[0]

        HEADER_COORDS = {
            "customer_name": (125.84, 110.15, 272.87, 122.43),
            "address": (125.84, 124.65, 347.06, 134.70),
            "invoice_no": (482.60, 110.13, 524.41, 120.18),
            "date": (479.85, 120.98, 523.99, 131.03),
            "license_no": (75.06, 181.90, 173.89, 191.95),
        }

        NTN_VALUE = (95, 158, 200, 168)
        SALES_TAX_VALUE = (110, 170, 220, 180)

        TABLE_COLS = {
            "sr": 54.7,
            "name": 72.1,
            "qty": 208.5,
            "batch": 249.7,
            "expiry": 330.3,
            "price": 388.6,
            "discount": 505.6,
            "amount": 546.0,
        }

        ROW_START_Y = 221.4
        ROW_HEIGHT = 9.5

        GROSS_VALUE_RECT = (535, 260, 590, 280)
        DISCOUNT_VALUE_RECT = (535, 277.58, 590, 287.63)
        NET_PAYABLE_RECT = (535, 320, 590, 345)
        COMPANY_TOTAL_RECT = (535, 240, 590, 260)

        data = {
            "customer_name": customer.name,
            "address": customer.address,
            "invoice_no": invoice.invoice_no,
            "date": now().strftime("%d/%m/%Y"),
            "license_no": invoice.license_no,
        }

        for rect in HEADER_COORDS.values():
            wipe_rect(page, rect)

        wipe_rect(page, NTN_VALUE)
        wipe_rect(page, SALES_TAX_VALUE)

        page.apply_redactions()

        for key, rect in HEADER_COORDS.items():
            write_in_rect(page, rect, data.get(key, ""), 9)

        write_in_rect(page, NTN_VALUE, customer.ntn, 9)
        write_in_rect(page, SALES_TAX_VALUE, customer.sales_tax, 9)

        table_rect = fitz.Rect(
            50,
            ROW_START_Y - 2,
            580,
            ROW_START_Y + (len(names) * ROW_HEIGHT) + 5
        )

        wipe_rect(page, table_rect)

        page.apply_redactions()

        for i in range(len(names)):

            if not names[i]:
                continue

            y = ROW_START_Y + i * ROW_HEIGHT

            qty = safe_decimal(qtys[i])
            price = safe_decimal(prices[i])
            disc = safe_decimal(discounts[i])

            discounted_price = (
                Decimal(price) -
                (
                    Decimal(price) *
                    Decimal(disc) /
                    Decimal("100")
                )
            )

            amount = discounted_price * Decimal(qty)

            page.insert_text((TABLE_COLS["sr"], y), str(i + 1), fontsize=8)
            page.insert_text((TABLE_COLS["name"], y), names[i], fontsize=8)
            page.insert_text((TABLE_COLS["qty"], y), str(qty), fontsize=8)
            page.insert_text((TABLE_COLS["batch"], y), batches[i], fontsize=8)
            page.insert_text((TABLE_COLS["expiry"], y), expiries[i], fontsize=8)
            page.insert_text((TABLE_COLS["price"], y), f"{price:.2f}", fontsize=8)
            page.insert_text((TABLE_COLS["discount"], y), f"{disc}%", fontsize=8)
            page.insert_text((TABLE_COLS["amount"], y), f"{amount:.2f}", fontsize=8)

        wipe_rect(page, GROSS_VALUE_RECT)
        wipe_rect(page, DISCOUNT_VALUE_RECT)
        wipe_rect(page, NET_PAYABLE_RECT)
        wipe_rect(page, COMPANY_TOTAL_RECT)

        page.apply_redactions()

        write_in_rect_right(page, GROSS_VALUE_RECT, f"{total_gross:.2f}", 9)
        write_in_rect_right(page, DISCOUNT_VALUE_RECT, f"-{abs(total_discount):.2f}", 9)
        write_in_rect_right(page, NET_PAYABLE_RECT, f"{total_net:.2f}", 9)
        write_in_rect_right(page, COMPANY_TOTAL_RECT, f"{total_net:.2f}", 9)

        pdf_bytes = io.BytesIO()

        doc.save(pdf_bytes)
        doc.close()

        pdf_bytes.seek(0)

        return FileResponse(
            pdf_bytes,
            as_attachment=True,
            filename=f"{invoice.invoice_no}.pdf",
            content_type="application/pdf"
        )

    return redirect("index")


# SUPER ADMIN LOGS VIEW
@login_required
def invoice_logs_view(request):

    if not is_super_admin(request.user):

        messages.error(
            request,
            "🚫 Access Denied! Just Super Admin accessable."
        )

        return redirect("index")

    # SUPER ADMINS DON'T GENERATE INVOICES, SO THEIR ROWS ARE NOISE
    logs = InvoiceLog.objects.exclude(
        user__userrolls__role=UserRolls.ROLE_SUPER_ADMIN
    ).select_related("invoice", "user").order_by("-timestamp")

    return render(
        request,
        "invoices/invoice_logs.html",
        {
            "logs": logs
        }
    )

# ---------------------------------------------------------------- LEDGERS

MONEY = DecimalField(max_digits=14, decimal_places=2)


def _sum_subquery(model, field):
    """Per-customer SUM as a correlated subquery.

    Two aggregates over different joins in one query multiply each other out,
    and Sum(distinct=True) is not a fix - it drops genuinely repeated amounts
    (two invoices of the same value would count once). Subqueries keep each
    total independent.
    """
    return Subquery(
        model.objects.filter(customer=OuterRef("pk"))
        .values("customer")
        .annotate(total=Sum(field))
        .values("total"),
        output_field=MONEY,
    )


def customers_with_balances(queryset=None):
    """Annotate customers with invoiced and paid totals."""
    customers = queryset if queryset is not None else Customer.objects.all()

    return customers.annotate(
        invoiced=Coalesce(_sum_subquery(Invoice, "total"), ZERO, output_field=MONEY),
        paid=Coalesce(_sum_subquery(Payment, "amount"), ZERO, output_field=MONEY),
    )


def overdue_invoices():
    cutoff = timezone.localdate() - timedelta(days=OVERDUE_DAYS)

    return (
        Invoice.objects.filter(date__lte=cutoff)
        .annotate(received=Coalesce(Sum("payments__amount"), ZERO))
        .filter(total__gt=F("received"))
        .select_related("customer")
        .order_by("date")
    )


@login_required
def dashboard(request):
    overdue = list(overdue_invoices())

    totals = Invoice.objects.aggregate(t=Sum("total"))["t"] or ZERO
    received = Payment.objects.aggregate(t=Sum("amount"))["t"] or ZERO

    return render(
        request,
        "invoices/dashboard.html",
        {
            "total_invoiced": totals,
            "total_received": received,
            "total_outstanding": totals - received,
            "overdue_invoices": overdue,
            "overdue_total": sum((i.balance for i in overdue), ZERO),
            "overdue_days": OVERDUE_DAYS,
            "customer_count": Customer.objects.count(),
            "invoice_count": Invoice.objects.count(),
        }
    )


@login_required
def ledger_list(request):
    query = request.GET.get("q", "").strip()

    customers = customers_with_balances()

    if query:
        customers = customers.filter(
            Q(name__icontains=query)
            | Q(address__icontains=query)
            | Q(license_no__icontains=query)
            | Q(contact_person__icontains=query)
        )

    rows = [
        {"customer": c, "invoiced": c.invoiced, "paid": c.paid,
         "balance": c.invoiced - c.paid}
        for c in customers
    ]

    # Biggest debtors first - that is what the page is for.
    rows.sort(key=lambda r: r["balance"], reverse=True)

    return render(
        request,
        "invoices/ledger_list.html",
        {
            "rows": rows,
            "query": query,
            "total_outstanding": sum((r["balance"] for r in rows), ZERO),
        }
    )


@login_required
def customer_ledger(request, customer_id):
    """A running statement: invoices as debits, payments as credits."""
    customer = get_object_or_404(Customer, pk=customer_id)

    entries = []

    for invoice in customer.invoice_set.all():
        entries.append({
            "date": invoice.date,
            "kind": "invoice",
            "reference": invoice.invoice_no,
            "detail": f"{invoice.items.count()} item(s)",
            "debit": invoice.total,
            "credit": ZERO,
            "object": invoice,
        })

    for payment in customer.payments.all():
        entries.append({
            "date": payment.paid_on,
            "kind": "payment",
            "reference": payment.reference or payment.get_method_display(),
            "detail": (
                f"Against {payment.invoice.invoice_no}"
                if payment.invoice else "Against account"
            ),
            "debit": ZERO,
            "credit": payment.amount,
            "object": payment,
        })

    entries.sort(key=lambda e: (e["date"], e["kind"] == "payment"))

    running = ZERO

    for entry in entries:
        running += entry["debit"] - entry["credit"]
        entry["balance"] = running

    return render(
        request,
        "invoices/customer_ledger.html",
        {
            "customer": customer,
            "entries": entries,
            "balance": running,
            "overdue": [i for i in customer.overdue_invoices()],
            "overdue_days": OVERDUE_DAYS,
        }
    )


@login_required
def payment_create(request, customer_id):
    customer = get_object_or_404(Customer, pk=customer_id)

    if request.method == "POST":
        form = PaymentForm(request.POST, customer=customer)

        if form.is_valid():
            payment = form.save(commit=False)
            payment.customer = customer
            payment.recorded_by = request.user
            payment.save()

            messages.success(
                request, f"Recorded {payment.amount} against {customer.name}."
            )

            return redirect("customer_ledger", customer_id=customer.pk)

    else:
        form = PaymentForm(customer=customer)

    return render(
        request,
        "invoices/payment_form.html",
        {
            "form": form,
            "customer": customer,
            "balance": customer.outstanding_balance,
        }
    )


@login_required
def payment_list(request):
    payments = Payment.objects.select_related("customer", "invoice", "recorded_by")

    return render(
        request,
        "invoices/payment_list.html",
        {
            "payments": payments,
            "total": payments.aggregate(t=Sum("amount"))["t"] or ZERO,
        }
    )


# ---------------------------------------------------------------- PROFILE / SEARCH

@login_required
def profile(request):
    profile_obj, _ = UserRolls.objects.get_or_create(user=request.user)

    if request.method == "POST":
        form = ProfileForm(request.POST, request.FILES, instance=profile_obj)

        if form.is_valid():
            form.save()
            messages.success(request, "Profile updated.")

            return redirect("profile")

    else:
        form = ProfileForm(instance=profile_obj)

    return render(
        request,
        "invoices/profile.html",
        {"form": form, "profile": profile_obj}
    )


@login_required
def global_search(request):
    """Header search across customer name, pharmacy address and licence."""
    query = request.GET.get("q", "").strip()

    customers = []
    invoices = []

    if query:
        customers = list(
            customers_with_balances(
                Customer.objects.filter(
                    Q(name__icontains=query)
                    | Q(address__icontains=query)
                    | Q(license_no__icontains=query)
                    | Q(contact_person__icontains=query)
                    | Q(contact_number__icontains=query)
                    | Q(ntn__icontains=query)
                )
            )
        )

        invoices = list(
            Invoice.objects.filter(
                Q(invoice_no__icontains=query)
                | Q(license_no__icontains=query)
                | Q(customer__name__icontains=query)
            ).select_related("customer")[:25]
        )

    return render(
        request,
        "invoices/search.html",
        {
            "query": query,
            "customers": customers,
            "invoices": invoices,
            "result_count": len(customers) + len(invoices),
        }
    )


# ---------------------------------------------------------------- TEAM & TERRITORY

@login_required
def territory_list(request):
    territories = Territory.objects.annotate(
        customer_count=Count("customers", distinct=True),
        call_point_count=Count("call_points", distinct=True),
        staff_count=Count("employees", distinct=True),
    )

    return render(
        request,
        "invoices/territory_list.html",
        {"territories": territories}
    )


@login_required
def territory_edit(request, territory_id=None):
    territory = get_object_or_404(Territory, pk=territory_id) if territory_id else None

    if request.method == "POST":
        form = TerritoryForm(request.POST, instance=territory)

        if form.is_valid():
            saved = form.save()
            messages.success(request, f"Saved territory {saved.name}.")

            return redirect("territory_list")

    else:
        form = TerritoryForm(instance=territory)

    return render(
        request,
        "invoices/simple_form.html",
        {
            "form": form,
            "heading": "Edit Territory" if territory else "New Territory",
            "cancel_url": reverse("territory_list"),
        }
    )


@login_required
def team_list(request):
    employees = Employee.objects.select_related(
        "territory", "reports_to", "user"
    )

    query = request.GET.get("q", "").strip()

    if query:
        employees = employees.filter(
            Q(full_name__icontains=query)
            | Q(employee_code__icontains=query)
            | Q(territory__name__icontains=query)
            | Q(territory__city__icontains=query)
        )

    return render(
        request,
        "invoices/team_list.html",
        {
            "employees": employees,
            "query": query,
        }
    )


@login_required
def employee_edit(request, employee_id=None):
    employee = get_object_or_404(Employee, pk=employee_id) if employee_id else None

    if request.method == "POST":
        form = EmployeeForm(request.POST, instance=employee)

        if form.is_valid():
            saved = form.save()
            messages.success(request, f"Saved {saved.full_name}.")

            return redirect("team_list")

    else:
        form = EmployeeForm(instance=employee)

    return render(
        request,
        "invoices/simple_form.html",
        {
            "form": form,
            "heading": "Edit Employee" if employee else "New Employee",
            "cancel_url": reverse("team_list"),
        }
    )


@login_required
def call_point_list(request):
    call_points = CallPoint.objects.select_related("territory", "customer")

    query = request.GET.get("q", "").strip()

    if query:
        call_points = call_points.filter(
            Q(name__icontains=query)
            | Q(speciality__icontains=query)
            | Q(address__icontains=query)
            | Q(territory__name__icontains=query)
        )

    return render(
        request,
        "invoices/call_point_list.html",
        {
            "call_points": call_points,
            "query": query,
        }
    )


@login_required
def call_point_edit(request, call_point_id=None):
    call_point = (
        get_object_or_404(CallPoint, pk=call_point_id) if call_point_id else None
    )

    if request.method == "POST":
        form = CallPointForm(request.POST, instance=call_point)

        if form.is_valid():
            saved = form.save()
            messages.success(request, f"Saved {saved.name}.")

            return redirect("call_point_list")

    else:
        form = CallPointForm(instance=call_point)

    return render(
        request,
        "invoices/simple_form.html",
        {
            "form": form,
            "heading": "Edit Call Point" if call_point else "New Call Point",
            "cancel_url": reverse("call_point_list"),
        }
    )


# ---------------------------------------------------------------- WEEKLY PLANS

@login_required
def plan_list(request):
    plans = WeeklyPlan.objects.select_related("employee", "employee__territory")

    return render(
        request,
        "invoices/plan_list.html",
        {
            "plans": plans,
            "form": PlanGenerateForm(initial={"week_start": current_week_start()}),
            "current_week": current_week_start(),
        }
    )


@login_required
def plan_generate(request):
    if request.method != "POST":
        return redirect("plan_list")

    form = PlanGenerateForm(request.POST)

    if not form.is_valid():
        messages.error(request, "Pick an employee and a week.")

        return redirect("plan_list")

    plan, created = generate_plan(
        employee=form.cleaned_data["employee"],
        week_start=form.cleaned_data["week_start"],
        calls_per_day=form.cleaned_data["calls_per_day"],
        created_by=request.user,
    )

    if not created:
        if not plan.is_editable:
            messages.error(
                request,
                f"That week is already {plan.get_status_display().lower()} - "
                f"it was left untouched.",
            )
        elif plan.employee.territory is None:
            messages.error(
                request,
                f"{plan.employee.full_name} has no territory assigned.",
            )
        else:
            messages.error(
                request,
                f"No active call points in {plan.employee.territory.name}.",
            )
    else:
        messages.success(request, f"Generated {created} visit(s).")

    return redirect("plan_detail", plan_id=plan.pk)


@login_required
def plan_detail(request, plan_id):
    plan = get_object_or_404(
        WeeklyPlan.objects.select_related("employee", "employee__territory"),
        pk=plan_id,
    )

    return render(
        request,
        "invoices/plan_detail.html",
        {
            "plan": plan,
            "days": plan.visits_by_day(),
        }
    )


@login_required
def plan_status(request, plan_id, action):
    plan = get_object_or_404(WeeklyPlan, pk=plan_id)

    transitions = {
        "submit": WeeklyPlan.STATUS_SUBMITTED,
        "approve": WeeklyPlan.STATUS_APPROVED,
        "reject": WeeklyPlan.STATUS_REJECTED,
    }

    if action not in transitions:
        messages.error(request, "Unknown action.")

        return redirect("plan_detail", plan_id=plan.pk)

    plan.status = transitions[action]

    if action in ("approve", "reject"):
        plan.reviewed_by = request.user
        plan.reviewed_at = timezone.now()
        plan.review_note = request.POST.get("review_note", "")

    plan.save()

    messages.success(request, f"Plan {plan.get_status_display().lower()}.")

    return redirect("plan_detail", plan_id=plan.pk)


@login_required
def visit_status(request, visit_id, action):
    """Field reporting: mark a planned call as done or missed."""
    visit = get_object_or_404(PlanVisit, pk=visit_id)

    if action in ("done", "missed", "planned"):
        visit.status = action
        visit.remarks = request.POST.get("remarks", visit.remarks)
        visit.save()

    return redirect("plan_detail", plan_id=visit.plan_id)


# ---------------------------------------------------------------- LOCATION REPORT

@login_required
def territory_report(request):
    """Sales, receivables and field coverage broken down by territory."""
    rows = []

    for territory in Territory.objects.all():
        customers = Customer.objects.filter(territory=territory)

        invoiced = (
            Invoice.objects.filter(customer__territory=territory)
            .aggregate(t=Sum("total"))["t"] or ZERO
        )
        received = (
            Payment.objects.filter(customer__territory=territory)
            .aggregate(t=Sum("amount"))["t"] or ZERO
        )

        rows.append({
            "territory": territory,
            "customers": customers.count(),
            "call_points": territory.call_points.filter(is_active=True).count(),
            "staff": territory.employees.filter(is_active=True).count(),
            "invoiced": invoiced,
            "received": received,
            "balance": invoiced - received,
        })

    rows.sort(key=lambda r: r["invoiced"], reverse=True)

    unassigned = Customer.objects.filter(territory__isnull=True).count()

    return render(
        request,
        "invoices/territory_report.html",
        {
            "rows": rows,
            "unassigned_customers": unassigned,
            "total_invoiced": sum((r["invoiced"] for r in rows), ZERO),
            "total_balance": sum((r["balance"] for r in rows), ZERO),
        }
    )
