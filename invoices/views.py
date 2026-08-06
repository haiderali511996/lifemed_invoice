from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse, JsonResponse
from django.urls import reverse
from django.contrib.auth.decorators import login_required
from django.contrib.auth import authenticate, login, logout
from django.utils.timezone import now
from django.contrib import messages

from django.db.models import Count, DecimalField, F, OuterRef, Q, Subquery, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.db import transaction

from datetime import timedelta

from .forms import (
    CallPointForm,
    CustomerForm,
    DistributorForm,
    EmployeeForm,
    PaymentForm,
    PlanGenerateForm,
    ProductForm,
    ProfileForm,
    PurchaseForm,
    StockAdjustmentForm,
    SupplierForm,
    TerritoryForm,
)
from .layout import LayoutError, describe, detect_layout
from .stock import StockError, adjust, allocate_fefo, issue, receive
from .planning import current_week_start, generate_plan
from .models import (
    Batch,
    CallPoint,
    Customer,
    Distributor,
    Employee,
    EXPIRY_WARNING_DAYS,
    Invoice,
    Item,
    InvoiceLog,
    Payment,
    PlanVisit,
    Product,
    Purchase,
    PurchaseItem,
    StockMovement,
    Supplier,
    Territory,
    UserRolls,
    WeeklyPlan,
    ZERO,
    OVERDUE_DAYS,
    is_super_admin,
)

from .pdf import TemplateError, render_invoice

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

# Column limits, so posted values are clamped rather than rejected by MySQL.
MAX_PRICE = "99999999.99"
MAX_DISCOUNT = "100.00"
MAX_TOTAL = "9999999999.99"

def safe_decimal(value, default="0.00", max_value=None):
    """Parse a posted number, never raising.

    MySQL runs in STRICT_TRANS_TABLES, so a value wider than its column is a
    hard error rather than a silent truncation. Everything written to the
    database is clamped here instead of failing the whole invoice.
    """
    try:
        text = str(value).strip()

        result = Decimal(default) if text == "" else Decimal(text)

        if not result.is_finite():
            return Decimal(default)

    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)

    if max_value is not None and result > Decimal(max_value):
        result = Decimal(max_value)

    return result.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def clip(value, length):
    """Trim a posted string to what its column can hold."""
    return str(value or "")[:length]


def safe_int(value, default=0, max_value=1_000_000):
    try:
        result = int(float(str(value).strip() or default))
    except (ValueError, TypeError, OverflowError):
        return default

    return max(0, min(result, max_value))


def previous_balance_breakdown(customer, current_invoice):
    """What the customer owed before this invoice, itemised by invoice number.

    Returns None when nothing is outstanding, so the block is left off the PDF
    entirely rather than printing a row of zeroes.
    """
    rows = []

    unpaid = (
        Invoice.objects.filter(customer=customer)
        .exclude(pk=current_invoice.pk)
        .order_by("date", "id")
    )

    for invoice in unpaid:
        if invoice.balance > ZERO:
            rows.append({
                "invoice_no": invoice.invoice_no,
                "date": invoice.date,
                "balance": invoice.balance,
            })

    # Authoritative figure: the account balance less the invoice just raised.
    # Payments recorded against the account rather than a specific invoice mean
    # the per-invoice balances can add up to more than is actually owed.
    total = customer.outstanding_balance - current_invoice.total

    if total <= ZERO and not rows:
        return None

    credit = sum((row["balance"] for row in rows), ZERO) - total

    return {
        "rows": rows,
        "credit": credit if credit > ZERO else None,
        "total": total,
        "grand_total": total + current_invoice.total,
    }


def _stock_batch(raw_id):
    """The Batch a row was picked from, if any. Free-text rows return None."""
    if not raw_id:
        return None

    return Batch.objects.select_related("product").filter(pk=raw_id).first()


def post_column(request, field, length):
    """Read one item column, padded to `length` so a short list can't IndexError.

    Browsers omit nothing here, but a hand-built or partially filled POST can
    send fewer batch/expiry values than item names.
    """
    values = request.POST.getlist(field)

    return values + [""] * (length - len(values))




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
            "customers": customers,
            "distributors": Distributor.objects.filter(is_active=True),
            "default_distributor": Distributor.default(),
            "products": Product.objects.filter(is_active=True),
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

        distributor = (
            Distributor.objects.filter(
                pk=request.POST.get("distributor"), is_active=True
            ).first()
            or Distributor.default()
        )

        license_no = clip(request.POST.get("license_no", ""), 100)

        customer_name = clip(request.POST.get("customer_name", ""), 255).strip()

        if not customer_name:
            messages.error(request, "Customer name is required.")

            return redirect("index")

        customer_fields = {
            "address": request.POST.get("address", ""),
            "ntn": clip(request.POST.get("ntn", ""), 50),
            "sales_tax": clip(request.POST.get("sales_tax", ""), 50),
            "license_no": license_no,
        }

        customer, created = Customer.objects.get_or_create(
            name=customer_name,
            defaults=customer_fields
        )

        if not created:
            for field, value in customer_fields.items():
                setattr(customer, field, value)

            customer.save()

        invoice = Invoice.objects.create(
            customer=customer,
            distributor=distributor,
            license_no=license_no
        )

        names = request.POST.getlist("item_name[]")
        row_count = len(names)

        qtys = post_column(request, "qty[]", row_count)
        prices = post_column(request, "price[]", row_count)
        discounts = post_column(request, "discount[]", row_count)
        batches = post_column(request, "batch[]", row_count)
        expiries = post_column(request, "expiry[]", row_count)
        batch_ids = post_column(request, "stock_batch[]", row_count)

        total_gross = Decimal("0")
        total_net = Decimal("0")
        total_discount = Decimal("0")

        pdf_rows = []

        # ITEMS LOOP
        for i in range(len(names)):

            if not names[i]:
                continue

            qty = safe_decimal(qtys[i], max_value=MAX_PRICE)
            price = safe_decimal(prices[i], max_value=MAX_PRICE)
            disc = safe_decimal(discounts[i], max_value=MAX_DISCOUNT)

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

            stock_batch = _stock_batch(batch_ids[i])

            item = Item.objects.create(
                invoice=invoice,
                name=clip(names[i], 255),
                qty=safe_int(qty),
                batch=clip(batches[i], 100) or (
                    stock_batch.batch_no if stock_batch else ""
                ),
                expiry=clip(expiries[i], 20),
                price=price,
                discount=disc,
                product=stock_batch.product if stock_batch else None,
                stock_batch=stock_batch,
            )

            if stock_batch is not None:
                try:
                    issue(
                        stock_batch,
                        item.qty,
                        reference=invoice.invoice_no,
                        user=request.user,
                    )

                except StockError as error:
                    # Selling stock that is not there would silently corrupt
                    # the ledger, so say so and leave the line unlinked.
                    messages.error(request, str(error))

                    Item.objects.filter(pk=item.pk).update(
                        product=None, stock_batch=None
                    )

            pdf_rows.append({
                "name": item.name,
                "qty": qty,
                "batch": item.batch,
                "expiry": item.expiry,
                "price": price,
                "discount": disc,
                "amount": amount,
            })

        # Store the net payable so ledgers never recompute it from line items
        invoice.total = safe_decimal(total_net, max_value=MAX_TOTAL)
        invoice.save(update_fields=["total"])

        # LOG ENTRY
        InvoiceLog.objects.create(
            invoice=invoice,
            user=request.user,
            customer_name=clip(customer.name, 255),
            amount=invoice.total,
            action="Invoice Created"
        )

        pdf_bytes = render_invoice(
            previous=previous_balance_breakdown(customer, invoice),
            header={
                "customer_name": customer.name,
                "address": customer.address,
                "invoice_no": invoice.invoice_no,
                "date": now().strftime("%d/%m/%Y"),
                "license_no": invoice.license_no,
                "ntn": customer.ntn or "",
                "sales_tax": customer.sales_tax or "",
                "area": customer.territory.city if customer.territory else "",
            },
            rows=pdf_rows,
            totals={
                "gross": total_gross,
                "discount": total_discount,
                "net": total_net,
            },
            distributor=distributor,
        )

        # Deliberately not FileResponse: it hands the object to the server's
        # wsgi.file_wrapper, and Passenger's implementation calls fileno() on
        # it. BytesIO has no file descriptor, so that raises
        # "io.UnsupportedOperation: fileno" and the download 500s under cPanel.
        response = HttpResponse(pdf_bytes, content_type="application/pdf")

        response["Content-Disposition"] = (
            f'attachment; filename="{invoice.invoice_no}.pdf"'
        )

        return response

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


# ---------------------------------------------------------------- DISTRIBUTORS

@login_required
def distributor_list(request):
    distributors = Distributor.objects.annotate(
        invoice_count=Count("invoices")
    )

    return render(
        request,
        "invoices/distributor_list.html",
        {"distributors": distributors}
    )


@login_required
def distributor_edit(request, distributor_id=None):
    distributor = (
        get_object_or_404(Distributor, pk=distributor_id) if distributor_id else None
    )

    previous_template = distributor.template.name if distributor else None

    if request.method == "POST":
        form = DistributorForm(request.POST, request.FILES, instance=distributor)

        if form.is_valid():
            saved = form.save()

            # Re-read coordinates whenever the form itself changes.
            if saved.template and saved.template.name != previous_template:
                _detect_and_store_layout(request, saved)

            messages.success(request, f"Saved {saved.name}.")

            return redirect("distributor_list")

    else:
        form = DistributorForm(instance=distributor)

    return render(
        request,
        "invoices/distributor_form.html",
        {
            "form": form,
            "distributor": distributor,
            "heading": "Edit Distributor" if distributor else "New Distributor",
        }
    )


def _detect_and_store_layout(request, distributor):
    """Read the coordinate map off a freshly uploaded template."""
    try:
        distributor.layout = detect_layout(distributor.template.path)
        distributor.save(update_fields=["layout"])

        summary = describe(distributor.layout)

        messages.success(
            request,
            f"Read {len(summary['fields'])} field(s), "
            f"{len(summary['columns'])} column(s) and "
            f"{len(summary['totals'])} total(s) from the template; "
            f"{summary['rows_per_page']} item row(s) fit per page.",
        )

        if summary["missing"]:
            messages.error(
                request,
                "Could not locate: " + ", ".join(summary["missing"])
                + ". Those fields will be left blank - check the template "
                  "labels, or set the coordinates by hand.",
            )

    except LayoutError as error:
        distributor.layout = None
        distributor.save(update_fields=["layout"])

        messages.error(request, f"Could not read the template: {error}")


@login_required
def distributor_detect(request, distributor_id):
    """Re-run detection, for a template that was replaced on disk."""
    distributor = get_object_or_404(Distributor, pk=distributor_id)

    if not distributor.template:
        messages.error(request, "Upload a template first.")
    else:
        _detect_and_store_layout(request, distributor)

    return redirect("distributor_layout", distributor_id=distributor.pk)


@login_required
def distributor_layout(request, distributor_id):
    """Show what was detected, so a human can sanity-check it."""
    distributor = get_object_or_404(Distributor, pk=distributor_id)

    return render(
        request,
        "invoices/distributor_layout.html",
        {
            "distributor": distributor,
            "summary": describe(distributor.layout) if distributor.has_layout else None,
        }
    )


@login_required
def distributor_preview(request, distributor_id):
    """A sample invoice on this distributor's form, to verify the mapping."""
    distributor = get_object_or_404(Distributor, pk=distributor_id)

    sample_rows = [
        {
            "name": f"Sample Product {i + 1}", "qty": 10, "batch": f"B-{i + 1}00",
            "expiry": "12/27", "price": Decimal("250.00"),
            "discount": Decimal("10.00"), "amount": Decimal("2250.00"),
        }
        for i in range(4)
    ]

    try:
        pdf_bytes = render_invoice(
            header={
                "customer_name": "SAMPLE PHARMACY",
                "address": "123 Sample Road, Lahore",
                "invoice_no": f"{distributor.code}-PREVIEW",
                "date": timezone.localdate().strftime("%d/%m/%Y"),
                "license_no": "LIC-SAMPLE-001",
                "ntn": "1234567-8",
                "sales_tax": "ST-SAMPLE",
                "area": "LAHORE",
            },
            rows=sample_rows,
            totals={
                "gross": Decimal("10000.00"),
                "discount": Decimal("1000.00"),
                "net": Decimal("9000.00"),
            },
            distributor=distributor,
        )

    except (TemplateError, LayoutError) as error:
        messages.error(request, str(error))

        return redirect("distributor_layout", distributor_id=distributor.pk)

    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = 'inline; filename="layout-preview.pdf"'

    return response


# ---------------------------------------------------------------- INVENTORY

@login_required
def product_list(request):
    query = request.GET.get("q", "").strip()

    products = Product.objects.all()

    if query:
        products = products.filter(
            Q(name__icontains=query) | Q(code__icontains=query)
        )

    rows = [
        {
            "product": product,
            "stock": product.stock_on_hand,
            "sellable": product.sellable_stock,
            "needs_reorder": product.needs_reorder,
        }
        for product in products
    ]

    return render(
        request,
        "invoices/product_list.html",
        {"rows": rows, "query": query}
    )


@login_required
def product_edit(request, product_id=None):
    product = get_object_or_404(Product, pk=product_id) if product_id else None

    if request.method == "POST":
        form = ProductForm(request.POST, instance=product)

        if form.is_valid():
            saved = form.save()
            messages.success(request, f"Saved {saved.name}.")

            return redirect("product_list")

    else:
        form = ProductForm(instance=product)

    return render(
        request,
        "invoices/simple_form.html",
        {
            "form": form,
            "heading": "Edit Product" if product else "New Product",
            "cancel_url": reverse("product_list"),
        }
    )


@login_required
def supplier_list(request):
    return render(
        request,
        "invoices/supplier_list.html",
        {"suppliers": Supplier.objects.annotate(purchase_count=Count("purchases"))}
    )


@login_required
def supplier_edit(request, supplier_id=None):
    supplier = get_object_or_404(Supplier, pk=supplier_id) if supplier_id else None

    if request.method == "POST":
        form = SupplierForm(request.POST, instance=supplier)

        if form.is_valid():
            saved = form.save()
            messages.success(request, f"Saved {saved.name}.")

            return redirect("supplier_list")

    else:
        form = SupplierForm(instance=supplier)

    return render(
        request,
        "invoices/simple_form.html",
        {
            "form": form,
            "heading": "Edit Supplier" if supplier else "New Supplier",
            "cancel_url": reverse("supplier_list"),
        }
    )


@login_required
def purchase_list(request):
    purchases = Purchase.objects.select_related("supplier").prefetch_related("items")

    return render(
        request,
        "invoices/purchase_list.html",
        {"purchases": purchases}
    )


@login_required
def purchase_create(request):
    """Receive goods: creates or tops up batches and moves stock in."""
    if request.method == "POST":
        form = PurchaseForm(request.POST)

        product_ids = request.POST.getlist("product[]")
        count = len(product_ids)

        batch_nos = post_column(request, "batch_no[]", count)
        expiries = post_column(request, "expiry_date[]", count)
        quantities = post_column(request, "quantity[]", count)
        costs = post_column(request, "cost_price[]", count)

        lines, errors = _clean_purchase_lines(
            product_ids, batch_nos, expiries, quantities, costs
        )

        if form.is_valid() and lines and not errors:
            purchase = form.save(commit=False)
            purchase.created_by = request.user
            purchase.save()

            _receive_lines(purchase, lines, request.user)

            messages.success(
                request,
                f"Received {len(lines)} line(s) from {purchase.supplier.name}.",
            )

            return redirect("purchase_list")

        for error in errors:
            messages.error(request, error)

        if not lines and not errors:
            messages.error(request, "Add at least one product line.")

    else:
        form = PurchaseForm(initial={"date": timezone.localdate()})

    return render(
        request,
        "invoices/purchase_form.html",
        {
            "form": form,
            "products": Product.objects.filter(is_active=True),
        }
    )


def _clean_purchase_lines(product_ids, batch_nos, expiries, quantities, costs):
    """Validate the posted rows before anything is written."""
    lines = []
    errors = []

    for index, raw_id in enumerate(product_ids):
        if not raw_id:
            continue

        product = Product.objects.filter(pk=raw_id).first()

        if product is None:
            errors.append(f"Row {index + 1}: unknown product.")
            continue

        batch_no = clip(batch_nos[index], 100).strip()

        if not batch_no:
            errors.append(f"Row {index + 1}: batch number is required.")
            continue

        expiry = parse_date(expiries[index])

        if expiry is None:
            errors.append(f"Row {index + 1}: a valid expiry date is required.")
            continue

        quantity = safe_int(quantities[index])

        if quantity <= 0:
            errors.append(f"Row {index + 1}: quantity must be at least 1.")
            continue

        lines.append({
            "product": product,
            "batch_no": batch_no,
            "expiry": expiry,
            "quantity": quantity,
            "cost": safe_decimal(costs[index], max_value=MAX_PRICE),
        })

    return lines, errors


@transaction.atomic
def _receive_lines(purchase, lines, user):
    for line in lines:
        batch, created = Batch.objects.get_or_create(
            product=line["product"],
            batch_no=line["batch_no"],
            defaults={
                "expiry_date": line["expiry"],
                "cost_price": line["cost"],
            },
        )

        if not created and batch.expiry_date != line["expiry"]:
            # Same batch number, different expiry: trust the delivery note.
            batch.expiry_date = line["expiry"]
            batch.cost_price = line["cost"]
            batch.save(update_fields=["expiry_date", "cost_price"])

        receive(
            batch,
            line["quantity"],
            reference=purchase.reference or f"GRN-{purchase.pk}",
            user=user,
        )

        PurchaseItem.objects.create(
            purchase=purchase,
            product=line["product"],
            batch=batch,
            quantity=line["quantity"],
            cost_price=line["cost"],
        )


@login_required
def stock_report(request):
    """Everything on hand, with expiry and reorder warnings."""
    batches = (
        Batch.objects.select_related("product")
        .filter(quantity__gt=0)
        .order_by("expiry_date")
    )

    today = timezone.localdate()
    soon = today + timedelta(days=EXPIRY_WARNING_DAYS)

    expired = [b for b in batches if b.expiry_date < today]
    expiring = [b for b in batches if today <= b.expiry_date <= soon]

    reorder = [p for p in Product.objects.filter(is_active=True) if p.needs_reorder]

    value = sum((b.cost_price * b.quantity for b in batches), ZERO)

    return render(
        request,
        "invoices/stock_report.html",
        {
            "batches": batches,
            "expired": expired,
            "expiring": expiring,
            "reorder": reorder,
            "stock_value": value,
            "expiry_days": EXPIRY_WARNING_DAYS,
        }
    )


@login_required
def stock_movements(request):
    movements = StockMovement.objects.select_related(
        "product", "batch", "created_by"
    )[:400]

    return render(
        request,
        "invoices/stock_movements.html",
        {"movements": movements}
    )


@login_required
def batch_adjust(request, batch_id):
    batch = get_object_or_404(Batch.objects.select_related("product"), pk=batch_id)

    if request.method == "POST":
        form = StockAdjustmentForm(request.POST)

        if form.is_valid():
            try:
                movement = adjust(
                    batch,
                    form.cleaned_data["counted_quantity"],
                    note=form.cleaned_data["note"],
                    user=request.user,
                )

            except StockError as error:
                messages.error(request, str(error))

            else:
                if movement is None:
                    messages.success(request, "Counted quantity already matched.")
                else:
                    messages.success(
                        request,
                        f"Adjusted {batch.product.name} / {batch.batch_no} "
                        f"by {movement.quantity:+d}.",
                    )

                return redirect("stock_report")

    else:
        form = StockAdjustmentForm(initial={"counted_quantity": batch.quantity})

    return render(
        request,
        "invoices/simple_form.html",
        {
            "form": form,
            "heading": f"Adjust {batch.product.name} / {batch.batch_no}",
            "cancel_url": reverse("stock_report"),
        }
    )


@login_required
def product_batches(request, product_id):
    """Sellable batches for a product, for the invoice form's row picker."""
    product = get_object_or_404(Product, pk=product_id)

    batches = (
        product.batches.filter(quantity__gt=0, expiry_date__gte=timezone.localdate())
        .order_by("expiry_date")
    )

    return JsonResponse({
        "product": product.name,
        "trade_price": f"{product.trade_price:.2f}",
        "batches": [
            {
                "id": batch.pk,
                "batch_no": batch.batch_no,
                "expiry": batch.expiry_date.strftime("%m/%y"),
                "quantity": batch.quantity,
            }
            for batch in batches
        ],
    })
