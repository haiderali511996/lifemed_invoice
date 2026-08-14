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
    CallReportForm,
    CapitalTransactionForm,
    CustomerForm,
    DistributorForm,
    EmployeeForm,
    ExpenseCategoryForm,
    ExpenseForm,
    ManufacturerForm,
    PartnerForm,
    PayrollRunForm,
    SampleIssueForm,
    PaymentForm,
    PlanGenerateForm,
    ProductForm,
    ProfileForm,
    PurchaseForm,
    BatchForm,
    StockAdjustmentForm,
    SupplierForm,
    TargetForm,
    TerritoryForm,
)
from . import finance
from .layout import LayoutError, describe, detect_layout
from .stock import (
    StockError, adjust, allocate_fefo, issue, receive, record_sales_return,
)
from .planning import current_week_start, generate_plan, monday_of
from django.contrib.auth.models import User

from .models import (
    Batch,
    CallPoint,
    CallReport,
    Customer,
    Distributor,
    Employee,
    Expense,
    ExpenseCategory,
    EXPIRY_WARNING_DAYS,
    Invoice,
    Item,
    InvoiceLog,
    Manufacturer,
    Partner,
    CapitalTransaction,
    Payment,
    PayrollRun,
    Payslip,
    PlanVisit,
    Product,
    SampleIssue,
    SampleIssueItem,
    SalesReturn,
    SalesReturnItem,
    Purchase,
    PurchaseItem,
    StockMovement,
    Supplier,
    Doctor,
    DoctorMove,
    Order,
    OrderItem,
    ProductTarget,
    Target,
    Territory,
    UserRolls,
    WeeklyPlan,
    ZERO,
    OVERDUE_DAYS,
    field_employee,
    is_field_staff,
    is_super_admin,
    normalise_address,
)

from .pdf import TemplateError, render_invoice
from .payslip import render_payslip

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


def previous_balance_breakdown(customer, current_invoice, as_at=None):
    """What the customer owed before this invoice, itemised by invoice number.

    Returns None when nothing is outstanding, so the block is left off the PDF
    entirely rather than printing a row of zeroes.

    Pass `as_at` to rebuild the figure as it stood at the start of a past day.
    A reprint uses it so the copy carries the same balance as the original,
    rather than today's — two copies of one invoice number that disagree about
    what was owed are worse than no copy at all.
    """
    rows = []

    unpaid = (
        Invoice.objects.filter(customer=customer)
        .exclude(pk=current_invoice.pk)
        .order_by("date", "id")
    )

    if as_at is not None:
        # Anything raised on or after the invoice date was not "previous".
        unpaid = unpaid.filter(date__lt=as_at)

    for invoice in unpaid:
        balance = (
            _balance_as_at(invoice, as_at) if as_at is not None
            else invoice.balance
        )

        if balance > ZERO:
            rows.append({
                "invoice_no": invoice.invoice_no,
                "date": invoice.date,
                "balance": balance,
            })

    # Authoritative figure: the account balance less the invoice just raised.
    # Payments recorded against the account rather than a specific invoice mean
    # the per-invoice balances can add up to more than is actually owed.
    total = (
        _account_balance_as_at(customer, as_at) if as_at is not None
        else customer.outstanding_balance - current_invoice.total
    )

    if total <= ZERO and not rows:
        return None

    credit = sum((row["balance"] for row in rows), ZERO) - total

    return {
        "rows": rows,
        "credit": credit if credit > ZERO else None,
        "total": total,
        "grand_total": total + current_invoice.total,
    }


def customer_for_invoice(name, address):
    """Which existing customer this invoice belongs to, or None to open one.

    Name and address together identify a customer. Invoicing the same name at
    a different address opens a second account rather than moving the first:
    two branches of one chain are two customers, each with its own balance,
    and overwriting the address would leave the earlier branch's invoices
    pointing at somewhere it never traded.

    The one exception is a customer whose address was never recorded. A blank
    address names no branch, so filling it in is completing a record rather
    than moving one - and refusing to would duplicate a customer over an
    address the operator simply had not typed yet. Two blanks under one name
    are ambiguous, so that case is left alone.
    """
    exact = Customer.at_address(name, address)

    if exact is not None:
        return exact

    if not normalise_address(address):
        return None

    unaddressed = [
        candidate
        for candidate in Customer.objects.filter(name__iexact=(name or "").strip())
        if not normalise_address(candidate.address)
    ]

    if len(unaddressed) != 1:
        return None

    customer = unaddressed[0]
    customer.address = address

    return customer


def deny_unless_mine(request, owner):
    """Stop a field login opening a record that belongs to someone else.

    The allowlist in access.py gates URLs; this gates the records behind them,
    so a legitimate URL with somebody else's id in it still goes nowhere.
    Returns a redirect to refuse, or None to allow.
    """
    mine = field_employee(request.user)

    if mine is None or owner == mine:
        return None

    messages.error(request, "🚫 That belongs to another team member.")

    return redirect("my_dashboard")


def _sales_rep_for(request, customer):
    """Who to credit this sale to.

    An explicit pick on the form wins. Otherwise it falls to whoever covers
    the customer's territory, so the common case needs no thought - and if
    two people cover it, nobody is credited by guesswork.
    """
    chosen = request.POST.get("sales_rep", "").strip()

    if chosen:
        return Employee.objects.filter(pk=chosen, is_active=True).first()

    if customer.territory_id is None:
        return None

    covering = Employee.objects.filter(
        territory_id=customer.territory_id, designation="mr", is_active=True
    )

    return covering.first() if covering.count() == 1 else None


def _balance_as_at(invoice, as_at):
    """One invoice's balance at the start of `as_at`.

    Credits are dated, so a payment or a return that landed later must not
    reduce the balance a past document reported.
    """
    paid = (
        invoice.payments.filter(paid_on__lt=as_at)
        .aggregate(t=Sum("amount"))["t"] or ZERO
    )
    returned = (
        invoice.returns.filter(date__lt=as_at)
        .aggregate(t=Sum("total"))["t"] or ZERO
    )

    return invoice.total - paid - returned


def _account_balance_as_at(customer, as_at):
    """Everything the account owed at the start of `as_at`.

    Account-level credits belong here and not in `_balance_as_at`, which is
    why the printed total is this figure rather than the sum of the rows.
    """
    invoiced = (
        customer.invoice_set.filter(date__lt=as_at)
        .aggregate(t=Sum("total"))["t"] or ZERO
    )
    paid = (
        customer.payments.filter(paid_on__lt=as_at)
        .aggregate(t=Sum("amount"))["t"] or ZERO
    )
    returned = (
        customer.returns.filter(date__lt=as_at)
        .aggregate(t=Sum("total"))["t"] or ZERO
    )

    return invoiced - paid - returned


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

            # SUPER ADMIN → LOGS PAGE, MR → THEIR OWN PORTAL,
            # EVERYONE ELSE → INVOICE FORM
            if is_super_admin(user):
                return redirect("invoice_logs")

            if is_field_staff(user):
                return redirect("my_dashboard")

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
def index(request, order_id=None):

    # SUPER ADMIN KO FORM NA DIKHAYE
    if is_super_admin(request.user):
        return redirect("invoice_logs")

    customers = Customer.objects.all()

    # Raising an invoice against an order: the form opens already filled in
    # with what the MR asked for, so the office is checking rather than
    # retyping. Nothing is committed until they submit, as usual.
    order = None

    if order_id is not None:
        order = get_object_or_404(
            Order.objects.select_related("customer", "employee")
            .prefetch_related("items__product"),
            pk=order_id,
        )

        if not order.can_invoice:
            messages.error(
                request,
                f"{order.order_no} is {order.get_status_display().lower()} "
                f"and cannot be invoiced.",
            )

            return redirect("order_detail", order_id=order.pk)

    return render(
        request,
        "invoices/index.html",
        {
            "active": "invoice",
            "customers": customers,
            "distributors": Distributor.objects.filter(is_active=True),
            "default_distributor": Distributor.default(),
            "products": Product.objects.filter(is_active=True),
            "sales_reps": Employee.objects.filter(
                is_active=True, designation="mr"
            ).select_related("territory"),
            "order": order,
            "order_lines": _order_lines(order),
        }
    )


def _order_lines(order):
    """An order's items in the shape index.html builds invoice rows from."""
    if order is None:
        return []

    return [
        {
            "name": line.product.name,
            "product_id": line.product_id,
            "qty": line.qty,
            "price": line.unit_price,
            "discount": line.discount,
            # addRow() assigns these straight onto inputs, so they have to be
            # present - undefined would render as the text "undefined".
            "batch": "",
            "expiry": "",
        }
        for line in order.items.all()
    ]


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
            "active": "customers",
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

        address = request.POST.get("address", "")

        registration = {
            "ntn": clip(request.POST.get("ntn", ""), 50),
            "sales_tax": clip(request.POST.get("sales_tax", ""), 50),
            "license_no": license_no,
        }

        customer = customer_for_invoice(customer_name, address)

        if customer is None:
            customer = Customer.objects.create(
                name=customer_name, address=address, **registration
            )
        else:
            # Same place, so the address stands as it is. The registration
            # details do change, and this invoice is the latest word on them.
            for field, value in registration.items():
                setattr(customer, field, value)

            customer.save()

        order = Order.objects.filter(
            pk=request.POST.get("order") or None
        ).first()

        invoice = Invoice.objects.create(
            customer=customer,
            distributor=distributor,
            license_no=license_no,
            # An order already names the MR who took it; crediting anyone else
            # would pay commission to the wrong person.
            sales_rep=(
                order.employee if order is not None
                else _sales_rep_for(request, customer)
            ),
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
                # Snapshotted now: receiving this batch number again later
                # overwrites its cost price, and the profit already earned on
                # this sale must not move when it does.
                unit_cost=stock_batch.cost_price if stock_batch else ZERO,
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

                    # No stock moved, so no cost was incurred either - leaving
                    # one behind would charge the partners for goods that
                    # never left the shelf.
                    Item.objects.filter(pk=item.pk).update(
                        product=None, stock_batch=None, unit_cost=ZERO
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
                # The invoice's own stored date, not now(): now() is UTC-aware
                # and formats to the UTC day, so between midnight and 5am here
                # the printed document was a day behind the ledger. One source
                # for the date means a reprint can never disagree either.
                "date": invoice.date.strftime("%d/%m/%Y"),
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

        _close_order(order, invoice, customer, request.user)

        return pdf_download(pdf_bytes, invoice.invoice_no)

    return redirect("index")


def _close_order(order, invoice, customer, user):
    """Mark the order invoiced and point it at what was raised.

    Also fills in the customer link if the order named a pharmacy that was
    not on the books yet - invoicing is what puts them there, and the order
    should point at the same record from then on.
    """
    if order is None or not order.can_invoice:
        return

    order.invoice = invoice
    order.status = Order.INVOICED
    order.reviewed_by = user
    order.reviewed_at = timezone.now()

    if order.customer_id is None:
        order.customer = customer

    order.save()


def pdf_download(pdf_bytes, name):
    """Send a PDF back as a download.

    Deliberately not FileResponse: it hands the object to the server's
    wsgi.file_wrapper, and Passenger's implementation calls fileno() on it.
    BytesIO has no file descriptor, so that raises
    "io.UnsupportedOperation: fileno" and the download 500s under cPanel.
    """
    response = HttpResponse(pdf_bytes, content_type="application/pdf")

    response["Content-Disposition"] = f'attachment; filename="{name}.pdf"'

    return response


def rebuild_invoice_pdf(invoice):
    """Redraw a stored invoice exactly as it was issued.

    Everything comes from the saved lines, the saved date and the distributor
    the invoice was raised on, so a reprint is a copy of the original document
    and not a fresh one that happens to share its number. Goods returned since
    stay off it too: a credit note is its own paperwork.
    """
    rows = []

    total_gross = ZERO
    total_discount = ZERO
    total_net = ZERO

    for item in invoice.items.all():
        # Same parse the original went through, so the copy prints the
        # quantity the same way ("2.00", not "2").
        qty = safe_decimal(item.qty, max_value=MAX_PRICE)
        price = Decimal(item.price)
        disc = Decimal(item.discount)

        gross = price * qty
        discount_amount = (price * disc / Decimal("100")) * qty
        amount = (price - (price * disc / Decimal("100"))) * qty

        total_gross += gross
        total_discount += discount_amount
        total_net += amount

        rows.append({
            "name": item.name,
            "qty": qty,
            "batch": item.batch or "",
            "expiry": item.expiry or "",
            "price": price,
            "discount": disc,
            "amount": amount,
        })

    customer = invoice.customer

    return render_invoice(
        previous=previous_balance_breakdown(
            customer, invoice, as_at=invoice.date
        ),
        header={
            "customer_name": customer.name,
            "address": customer.address,
            "invoice_no": invoice.invoice_no,
            "date": invoice.date.strftime("%d/%m/%Y"),
            "license_no": invoice.license_no,
            "ntn": customer.ntn or "",
            "sales_tax": customer.sales_tax or "",
            "area": customer.territory.city if customer.territory else "",
        },
        rows=rows,
        totals={
            "gross": total_gross,
            "discount": total_discount,
            "net": total_net,
        },
        distributor=invoice.distributor,
    )


@login_required
def invoice_list(request):
    """Every invoice raised, so any of them can be printed again."""
    invoices = Invoice.objects.select_related(
        "customer", "distributor"
    ).prefetch_related("items", "payments", "returns")

    query = request.GET.get("q", "").strip()
    distributor_id = request.GET.get("distributor", "").strip()
    status = request.GET.get("status", "").strip()

    if query:
        invoices = invoices.filter(
            Q(invoice_no__icontains=query)
            | Q(customer__name__icontains=query)
            | Q(license_no__icontains=query)
        )

    if distributor_id:
        invoices = invoices.filter(distributor_id=distributor_id)

    invoices = list(invoices)

    if status == "unpaid":
        invoices = [i for i in invoices if i.balance > ZERO]
    elif status == "paid":
        invoices = [i for i in invoices if i.balance <= ZERO]

    return render(
        request,
        "invoices/invoice_list.html",
        {
            "active": "invoices",
            "invoices": invoices,
            "distributors": Distributor.objects.filter(is_active=True),
            "query": query,
            "selected_distributor": distributor_id,
            "status": status,
            # Net of credit notes - returned goods were never a completed sale.
            "total": sum((i.total - i.amount_returned for i in invoices), ZERO),
            "outstanding": sum(
                (i.balance for i in invoices if i.balance > ZERO), ZERO
            ),
        }
    )


@login_required
def invoice_reprint(request, invoice_id):
    """Download a stored invoice again, unchanged and under its own number."""
    invoice = get_object_or_404(
        Invoice.objects.select_related("customer", "distributor"), pk=invoice_id
    )

    pdf_bytes = rebuild_invoice_pdf(invoice)

    # Reprints are worth a trail: a second copy of an invoice in circulation
    # is a collection risk, so it should be as visible as the original.
    InvoiceLog.objects.create(
        invoice=invoice,
        user=request.user,
        customer_name=clip(invoice.customer.name, 255),
        amount=invoice.total,
        action="Invoice Reprinted",
    )

    return pdf_download(pdf_bytes, invoice.invoice_no)


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
            "active": "logs",
            "logs": logs
        }
    )

# ---------------------------------------------------------------- LEDGERS

MONEY = DecimalField(max_digits=14, decimal_places=2)


def _sum_subquery(model, field, parent="customer"):
    """Per-customer SUM as a correlated subquery.

    Two aggregates over different joins in one query multiply each other out,
    and Sum(distinct=True) is not a fix - it drops genuinely repeated amounts
    (two invoices of the same value would count once). Subqueries keep each
    total independent.
    """
    return Subquery(
        model.objects.filter(**{parent: OuterRef("pk")})
        .values(parent)
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
        returned=Coalesce(
            _sum_subquery(SalesReturn, "total"), ZERO, output_field=MONEY
        ),
    )


def overdue_invoices():
    """Unpaid past the threshold, net of anything credited back."""
    cutoff = timezone.localdate() - timedelta(days=OVERDUE_DAYS)

    settled = Coalesce(
        _sum_subquery(Payment, "amount", "invoice"), ZERO, output_field=MONEY
    ) + Coalesce(
        _sum_subquery(SalesReturn, "total", "invoice"), ZERO, output_field=MONEY
    )

    return (
        Invoice.objects.filter(date__lte=cutoff)
        .annotate(settled=settled)
        .filter(total__gt=F("settled"))
        .select_related("customer")
        .order_by("date")
    )


@login_required
def dashboard(request):
    overdue = list(overdue_invoices())

    # Net of credit notes: goods a customer sent back were never really a
    # sale, so counting them here would overstate both what was invoiced and,
    # since Outstanding is derived from it below, what is still owed.
    gross = Invoice.objects.aggregate(t=Sum("total"))["t"] or ZERO
    returned = SalesReturn.objects.aggregate(t=Sum("total"))["t"] or ZERO
    totals = gross - returned

    received = Payment.objects.aggregate(t=Sum("amount"))["t"] or ZERO

    # An all-time total only ever grows, so on its own it says nothing about
    # how the business is doing now. These give the figures something to be
    # measured against.
    recent = finance.month_to_date()

    return render(
        request,
        "invoices/dashboard.html",
        {
            "active": "dashboard",
            "total_invoiced": totals,
            "total_received": received,
            "total_outstanding": totals - received,
            "overdue_invoices": overdue,
            "overdue_total": sum((i.balance for i in overdue), ZERO),
            "overdue_days": OVERDUE_DAYS,
            "customer_count": Customer.objects.count(),
            "invoice_count": Invoice.objects.count(),
            "recent": recent,
            "expiry": finance.expiry_exposure(),
            "collected_share": (
                (received * Decimal("100") / totals).quantize(Decimal("0.1"))
                if totals else ZERO
            ),
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
         "returned": c.returned,
         "balance": c.invoiced - c.paid - c.returned}
        for c in customers
    ]

    # Biggest debtors first - that is what the page is for.
    rows.sort(key=lambda r: r["balance"], reverse=True)

    return render(
        request,
        "invoices/ledger_list.html",
        {
            "active": "ledgers",
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

    for credit_note in customer.returns.select_related("invoice"):
        entries.append({
            "date": credit_note.date,
            "kind": "return",
            "reference": credit_note.return_no,
            "detail": f"Return against {credit_note.invoice.invoice_no}",
            "debit": ZERO,
            "credit": credit_note.total,
            "object": credit_note,
        })

    entries.sort(key=lambda e: (e["date"], e["kind"] != "invoice"))

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
            "active": "payments",
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
    products = []

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

        products = list(
            Product.objects.filter(
                Q(name__icontains=query)
                | Q(code__icontains=query)
                | Q(generic_name__icontains=query)
                | Q(manufacturer__name__icontains=query)
            ).select_related("manufacturer")[:25]
        )

    return render(
        request,
        "invoices/search.html",
        {
            "query": query,
            "customers": customers,
            "invoices": invoices,
            "products": products,
            "result_count": len(customers) + len(invoices) + len(products),
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
        {"active": "territories", "territories": territories}
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
            "active": "team",
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

    mine = field_employee(request.user)

    if mine is not None:
        # An MR works one patch, so this is their doctor list, not the company's.
        call_points = call_points.filter(territory=mine.territory)

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
            "active": "call_points",
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

    mine = field_employee(request.user)

    if mine is not None:
        plans = plans.filter(employee=mine)

    return render(
        request,
        "invoices/plan_list.html",
        {
            "active": "plans",
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


def plan_days(plan):
    """Each day of a plan week: what was scheduled, and what actually happened.

    Reports are matched by date rather than by scheduled slot, so a call made
    off the plan still counts towards the day's work - which is the number a
    manager actually wants.
    """
    if plan is None:
        return []

    visits = list(
        plan.visits.select_related("call_point").prefetch_related("report")
    )

    week_end = plan.week_start + timedelta(days=6)

    reports = list(
        CallReport.objects.filter(
            employee=plan.employee,
            visit_date__gte=plan.week_start,
            visit_date__lte=week_end,
        ).select_related("call_point", "sample_issue")
    )

    days = []

    for day, label in PlanVisit.DAY_CHOICES:
        on = plan.week_start + timedelta(days=day)

        planned = [v for v in visits if v.day == day]
        made = [r for r in reports if r.visit_date == on]

        days.append({
            "day": day,
            "label": label,
            "date": on,
            "visits": planned,
            "reports": made,
            "planned_count": len(planned),
            "made_count": len(made),
            "met_count": len([r for r in made if r.outcome == CallReport.MET]),
            "unplanned_count": len([r for r in made if not r.was_planned]),
            "samples": sum(r.samples_given for r in made),
            "coverage": (
                round(len(made) * 100 / len(planned)) if planned else 0
            ),
        })

    return days


@login_required
def plan_detail(request, plan_id):
    plan = get_object_or_404(
        WeeklyPlan.objects.select_related("employee", "employee__territory"),
        pk=plan_id,
    )

    refused = deny_unless_mine(request, plan.employee)

    if refused is not None:
        return refused

    days = plan_days(plan)

    return render(
        request,
        "invoices/plan_detail.html",
        {
            "plan": plan,
            "days": days,
            "made_total": sum(day["made_count"] for day in days),
            "samples_total": sum(day["samples"] for day in days),
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
    visit = get_object_or_404(
        PlanVisit.objects.select_related("plan__employee"), pk=visit_id
    )

    refused = deny_unless_mine(request, visit.plan.employee)

    if refused is not None:
        return refused

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

        # Net of credit notes - a returned delivery was never a completed
        # sale, and leaving it in would overstate both Invoiced and, since
        # Outstanding is derived from it below, what the territory is owed.
        gross_invoiced = (
            Invoice.objects.filter(customer__territory=territory)
            .aggregate(t=Sum("total"))["t"] or ZERO
        )
        returned = (
            SalesReturn.objects.filter(customer__territory=territory)
            .aggregate(t=Sum("total"))["t"] or ZERO
        )
        invoiced = gross_invoiced - returned

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
            "active": "territory_report",
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
        {"active": "distributors", "distributors": distributors}
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

    products = Product.objects.select_related("manufacturer")

    maker = request.GET.get("manufacturer", "").strip()

    if maker:
        products = products.filter(manufacturer_id=maker)

    if query:
        products = products.filter(
            Q(name__icontains=query)
            | Q(code__icontains=query)
            | Q(generic_name__icontains=query)
            | Q(manufacturer__name__icontains=query)
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
        {
            "active": "products",
            "rows": rows,
            "query": query,
            "manufacturers": Manufacturer.objects.filter(is_active=True),
            "selected_manufacturer": maker,
        }
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
        {
            "active": "suppliers",
            "suppliers": Supplier.objects.annotate(purchase_count=Count("purchases")),
        }
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
        {"active": "purchases", "purchases": purchases}
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
            "active": "stock",
            # Valued, not counted: "three batches" does not say whether that
            # is a rounding error or a month's profit about to be binned.
            "expiry": finance.expiry_exposure(EXPIRY_WARNING_DAYS),
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
def batch_edit(request, batch_id):
    """Correct a batch number or an expiry date entered wrongly.

    Expiry belongs to the batch, not the product: one product arrives in many
    lots, each with its own date, which is why there is no expiry field on the
    product screen.
    """
    batch = get_object_or_404(Batch.objects.select_related("product"), pk=batch_id)

    # Captured before the form runs: validating a ModelForm writes the posted
    # values straight onto the instance, so by then the old ones are gone.
    was = (batch.batch_no, batch.expiry_date)

    if request.method == "POST":
        form = BatchForm(request.POST, instance=batch)

        if form.is_valid():
            saved = form.save()

            record_batch_correction(saved, was, request.user)

            messages.success(
                request,
                f"Updated {saved.product.name} / {saved.batch_no}"
                f" — expires {saved.expiry_date:%d-%m-%Y}.",
            )

            return redirect("stock_report")

    else:
        form = BatchForm(instance=batch)

    return render(
        request,
        "invoices/batch_form.html",
        {
            "form": form,
            "batch": batch,
        }
    )


def _date_or_blank(value):
    return f"{value:%d-%m-%Y}" if value else "not set"


def record_batch_correction(batch, was, user):
    """Note a changed batch number or expiry in the stock ledger.

    Expiry drives what may be sold and the order stock goes out in, so a
    silent edit is not good enough for a pharmaceutical business. The entry
    moves no stock - it carries a quantity of zero - and exists to say who
    changed what, and when.
    """
    old_number, old_expiry = was

    changes = []

    if old_number != batch.batch_no:
        changes.append(f"batch number {old_number} → {batch.batch_no}")

    if old_expiry != batch.expiry_date:
        # MySQL can hand back a zero date as None for rows written before the
        # column was constrained, and formatting None raises. Say "not set"
        # rather than losing the whole correction to a TypeError.
        changes.append(
            f"expiry {_date_or_blank(old_expiry)} → "
            f"{_date_or_blank(batch.expiry_date)}"
        )

    if not changes:
        return None

    return StockMovement.objects.create(
        product=batch.product,
        batch=batch,
        quantity=0,
        kind=StockMovement.ADJUSTMENT,
        note="Corrected " + ", ".join(changes),
        created_by=user,
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


# ---------------------------------------------------------------- MANUFACTURERS

@login_required
def manufacturer_list(request):
    query = request.GET.get("q", "").strip()

    # Not "products": that name is already taken by the reverse relation.
    manufacturers = Manufacturer.objects.annotate(
        product_total=Count("products", distinct=True)
    )

    if query:
        manufacturers = manufacturers.filter(
            Q(name__icontains=query)
            | Q(code__icontains=query)
            | Q(contact_person__icontains=query)
            | Q(country__icontains=query)
        )

    return render(
        request,
        "invoices/manufacturer_list.html",
        {"active": "manufacturers", "manufacturers": manufacturers, "query": query}
    )


@login_required
def manufacturer_edit(request, manufacturer_id=None):
    manufacturer = (
        get_object_or_404(Manufacturer, pk=manufacturer_id)
        if manufacturer_id else None
    )

    if request.method == "POST":
        form = ManufacturerForm(request.POST, instance=manufacturer)

        if form.is_valid():
            saved = form.save()
            messages.success(request, f"Saved {saved.name}.")

            return redirect("manufacturer_detail", manufacturer_id=saved.pk)

    else:
        form = ManufacturerForm(instance=manufacturer)

    return render(
        request,
        "invoices/simple_form.html",
        {
            "form": form,
            "heading": (
                f"Edit {manufacturer.name}" if manufacturer
                else "New Manufacturer"
            ),
            "cancel_url": reverse("manufacturer_list"),
        }
    )


@login_required
def manufacturer_detail(request, manufacturer_id):
    """Everything this manufacturer makes, and what is on hand."""
    manufacturer = get_object_or_404(Manufacturer, pk=manufacturer_id)

    rows = [
        {
            "product": product,
            "stock": product.stock_on_hand,
            "sellable": product.sellable_stock,
            "needs_reorder": product.needs_reorder,
        }
        for product in manufacturer.products.all()
    ]

    return render(
        request,
        "invoices/manufacturer_detail.html",
        {
            "manufacturer": manufacturer,
            "rows": rows,
            "total_stock": sum(row["stock"] for row in rows),
        }
    )


# ---------------------------------------------------------------- SALES RETURNS

@login_required
def return_list(request):
    returns = SalesReturn.objects.select_related(
        "customer", "invoice", "created_by"
    ).prefetch_related("items")

    return render(
        request,
        "invoices/return_list.html",
        {
            "active": "returns",
            "returns": returns,
            "total": returns.aggregate(t=Sum("total"))["t"] or ZERO,
        }
    )


@login_required
def return_create(request, invoice_id):
    """Credit an invoice and put its goods back, without deleting anything."""
    invoice = get_object_or_404(
        Invoice.objects.select_related("customer"), pk=invoice_id
    )

    lines = []

    for item in invoice.items.select_related("stock_batch", "product"):
        already = invoice.returned_qty(item)

        lines.append({
            "item": item,
            "already_returned": already,
            "returnable": max(0, item.qty - already),
        })

    if request.method == "POST":
        selected, errors = _clean_return_lines(request, lines)

        if not selected and not errors:
            errors.append("Enter a quantity against at least one line.")

        if errors:
            for error in errors:
                messages.error(request, error)

        else:
            sales_return = SalesReturn.objects.create(
                invoice=invoice,
                customer=invoice.customer,
                date=parse_date(request.POST.get("date", "")) or timezone.localdate(),
                reason=request.POST.get("reason", "").strip(),
                restock=request.POST.get("restock") == "on",
                created_by=request.user,
            )

            created, restocked = record_sales_return(
                sales_return, selected, user=request.user
            )

            messages.success(
                request,
                f"{sales_return.return_no}: credited {sales_return.total} "
                f"across {created} line(s)"
                + (f", {restocked} unit(s) back in stock." if restocked
                   else " (not restocked).")
            )

            return redirect("customer_ledger", customer_id=invoice.customer_id)

    return render(
        request,
        "invoices/return_form.html",
        {
            "invoice": invoice,
            "lines": lines,
            "today": timezone.localdate(),
            "fully_returned": all(line["returnable"] == 0 for line in lines),
        }
    )


def _clean_return_lines(request, lines):
    """Validate quantities against what is actually still returnable."""
    selected = []
    errors = []

    for index, line in enumerate(lines):
        raw = request.POST.get(f"qty_{line['item'].pk}", "").strip()

        if not raw:
            continue

        qty = safe_int(raw)

        if qty <= 0:
            continue

        if qty > line["returnable"]:
            errors.append(
                f"{line['item'].name}: only {line['returnable']} left to "
                f"return (sold {line['item'].qty}, already returned "
                f"{line['already_returned']})."
            )
            continue

        selected.append({"item": line["item"], "qty": qty})

    return selected, errors


# ---------------------------------------------------------------- STOCK LEDGER

@login_required
def stock_ledger(request):
    """Running balance per batch, the stock equivalent of a customer statement."""
    movements = StockMovement.objects.select_related(
        "product", "batch", "created_by"
    ).order_by("created_at", "id")

    product_id = request.GET.get("product", "").strip()
    batch_id = request.GET.get("batch", "").strip()

    if product_id:
        movements = movements.filter(product_id=product_id)

    if batch_id:
        movements = movements.filter(batch_id=batch_id)

    running = {}
    entries = []

    for movement in movements:
        key = movement.batch_id
        running[key] = running.get(key, 0) + movement.quantity

        entries.append({
            "movement": movement,
            "balance": running[key],
        })

    entries.reverse()

    return render(
        request,
        "invoices/stock_ledger.html",
        {
            "active": "stock_ledger",
            "entries": entries,
            "products": Product.objects.filter(is_active=True),
            "selected_product": product_id,
            "total_in": sum(
                e["movement"].quantity for e in entries
                if e["movement"].quantity > 0
            ),
            "total_out": sum(
                -e["movement"].quantity for e in entries
                if e["movement"].quantity < 0
            ),
        }
    )


# ---------------------------------------------------------------- PURCHASE EDIT

@login_required
def purchase_edit(request, purchase_id):
    """Correct a received purchase.

    Changing a cost restates the batch; changing a quantity moves stock by the
    difference rather than overwriting it, so the ledger stays truthful.
    """
    purchase = get_object_or_404(
        Purchase.objects.select_related("supplier"), pk=purchase_id
    )

    items = list(
        purchase.items.select_related("product", "batch").order_by("id")
    )

    if request.method == "POST":
        form = PurchaseForm(request.POST, instance=purchase)

        changes, errors = _clean_purchase_edits(request, items)

        if form.is_valid() and not errors:
            form.save()

            applied = _apply_purchase_edits(changes, request.user)

            messages.success(
                request,
                f"Updated {len(changes)} line(s)."
                + (f" Stock adjusted by {applied:+d} unit(s)." if applied else "")
            )

            return redirect("purchase_list")

        for error in errors:
            messages.error(request, error)

    else:
        form = PurchaseForm(instance=purchase)

    return render(
        request,
        "invoices/purchase_edit.html",
        {
            "form": form,
            "purchase": purchase,
            "items": items,
        }
    )


def _clean_purchase_edits(request, items):
    changes = []
    errors = []

    for item in items:
        cost = safe_decimal(
            request.POST.get(f"cost_{item.pk}", item.cost_price),
            max_value=MAX_PRICE,
        )
        qty = safe_int(request.POST.get(f"qty_{item.pk}", item.quantity))

        if qty <= 0:
            errors.append(f"{item.product.name}: quantity must be at least 1.")
            continue

        delta = qty - item.quantity

        # Reducing a receipt below what is left on the shelf would drive the
        # batch negative - that stock has already been sold.
        if delta < 0 and item.batch.quantity + delta < 0:
            errors.append(
                f"{item.product.name} batch {item.batch.batch_no}: only "
                f"{item.batch.quantity} in stock, cannot reduce the receipt "
                f"by {abs(delta)}."
            )
            continue

        raw_expiry = request.POST.get(f"expiry_{item.pk}")

        if raw_expiry is None:
            # The field was not submitted at all - leave the batch as it is
            # rather than refusing an otherwise good correction.
            expiry = item.batch.expiry_date
        else:
            expiry = parse_date(raw_expiry)

            if expiry is None:
                errors.append(
                    f"{item.product.name} batch {item.batch.batch_no}: "
                    f"enter a valid expiry date."
                )
                continue

        if delta or cost != item.cost_price or expiry != item.batch.expiry_date:
            changes.append({
                "item": item, "cost": cost, "qty": qty,
                "delta": delta, "expiry": expiry,
            })

    return changes, errors


@transaction.atomic
def _apply_purchase_edits(changes, user):
    applied = 0

    for change in changes:
        item = change["item"]
        batch = item.batch

        if change["delta"]:
            Batch.objects.filter(pk=batch.pk).update(
                quantity=F("quantity") + change["delta"],
                received_quantity=F("received_quantity") + change["delta"],
            )

            StockMovement.objects.create(
                product=item.product,
                batch=batch,
                quantity=change["delta"],
                kind=StockMovement.ADJUSTMENT,
                reference=item.purchase.reference or f"GRN-{item.purchase_id}",
                note="Purchase quantity corrected",
                created_by=user,
            )

            applied += change["delta"]

        item.quantity = change["qty"]
        item.cost_price = change["cost"]
        item.save(update_fields=["quantity", "cost_price"])

        if batch.cost_price != change["cost"]:
            batch.cost_price = change["cost"]
            batch.save(update_fields=["cost_price"])

        # A mistyped expiry is corrected here rather than only on the batch
        # screen, because receiving is where the typo happens.
        if change["expiry"] != batch.expiry_date:
            was = (batch.batch_no, batch.expiry_date)

            batch.expiry_date = change["expiry"]
            batch.save(update_fields=["expiry_date"])

            record_batch_correction(batch, was, user)

    return applied


# ---------------------------------------------------------------- EXPENSES

@login_required
def expense_list(request):
    expenses = Expense.objects.select_related(
        "category", "employee", "territory", "submitted_by"
    )

    category_id = request.GET.get("category", "").strip()
    employee_id = request.GET.get("employee", "").strip()
    status = request.GET.get("status", "").strip()
    month = request.GET.get("month", "").strip()

    if category_id:
        expenses = expenses.filter(category_id=category_id)

    mine = field_employee(request.user)

    # Anyone linked to a team member can narrow the page to their own claims;
    # a field login is always narrowed and cannot widen it again.
    me = mine or Employee.objects.filter(user=request.user).first()
    only_mine = mine is not None or request.GET.get("mine") == "1"

    if mine is not None:
        expenses = expenses.filter(employee=mine)
    elif only_mine and me is not None:
        expenses = expenses.filter(employee=me)
    elif employee_id:
        expenses = expenses.filter(employee_id=employee_id)

    if status:
        expenses = expenses.filter(status=status)

    if month:
        parsed = parse_date(f"{month}-01")

        if parsed:
            expenses = expenses.filter(
                date__year=parsed.year, date__month=parsed.month
            )

    expenses = list(expenses)

    # Rejected claims never leave the bank, so they are not spend.
    counted = [e for e in expenses if e.counts_towards_spend]

    return render(
        request,
        "invoices/expense_list.html",
        {
            "active": "expenses",
            "expenses": expenses,
            "total": sum((e.amount for e in counted), ZERO),
            "pending_total": sum(
                (e.amount for e in expenses if e.status == Expense.PENDING), ZERO
            ),
            "categories": ExpenseCategory.objects.filter(is_active=True),
            "employees": Employee.objects.filter(is_active=True),
            "statuses": Expense.STATUS_CHOICES,
            "me": me,
            "only_mine": only_mine,
            "locked_to_me": mine is not None,
            "selected": {
                "category": category_id, "employee": employee_id,
                "status": status, "month": month,
            },
        }
    )


@login_required
def expense_edit(request, expense_id=None):
    expense = get_object_or_404(Expense, pk=expense_id) if expense_id else None

    if expense is not None:
        refused = deny_unless_mine(request, expense.employee)

        if refused is not None:
            return refused

    if request.method == "POST":
        form = ExpenseForm(request.POST, request.FILES, instance=expense)

        if form.is_valid():
            saved = form.save(commit=False)

            if saved.submitted_by_id is None:
                saved.submitted_by = request.user

            mine = field_employee(request.user)

            if mine is not None:
                # An MR claims for themselves, never on another's behalf, and
                # a claim always starts pending whoever typed it.
                saved.employee = mine
                saved.status = Expense.PENDING

            saved.save()

            messages.success(
                request, f"Saved {saved.category.name} — {saved.amount}."
            )

            return redirect("expense_list")

    else:
        form = ExpenseForm(instance=expense)

    return render(
        request,
        "invoices/expense_form.html",
        {
            "form": form,
            "expense": expense,
            "heading": "Edit Expense" if expense else "New Expense",
        }
    )


@login_required
def expense_status(request, expense_id, action):
    expense = get_object_or_404(Expense, pk=expense_id)

    transitions = {
        "approve": Expense.APPROVED,
        "reject": Expense.REJECTED,
        "paid": Expense.PAID,
    }

    if action not in transitions:
        messages.error(request, "Unknown action.")

    else:
        expense.status = transitions[action]
        expense.reviewed_by = request.user
        expense.reviewed_at = timezone.now()
        expense.review_note = request.POST.get("review_note", "")[:255]
        expense.save()

        messages.success(
            request, f"{expense.category.name} marked {expense.get_status_display().lower()}."
        )

    return redirect("expense_list")


@login_required
def expense_report(request):
    """Spend by category and by team member, for a chosen month or all time."""
    month = request.GET.get("month", "").strip()

    expenses = Expense.objects.exclude(status=Expense.REJECTED).select_related(
        "category", "employee"
    )

    if month:
        parsed = parse_date(f"{month}-01")

        if parsed:
            expenses = expenses.filter(
                date__year=parsed.year, date__month=parsed.month
            )

    by_category = {}
    by_employee = {}

    for expense in expenses:
        by_category[expense.category.name] = (
            by_category.get(expense.category.name, ZERO) + expense.amount
        )

        who = expense.employee.full_name if expense.employee else "Company"
        by_employee[who] = by_employee.get(who, ZERO) + expense.amount

    return render(
        request,
        "invoices/expense_report.html",
        {
            "active": "expense_report",
            "by_category": sorted(
                by_category.items(), key=lambda row: row[1], reverse=True
            ),
            "by_employee": sorted(
                by_employee.items(), key=lambda row: row[1], reverse=True
            ),
            "total": sum(by_category.values(), ZERO),
            "month": month,
        }
    )


@login_required
def expense_category_list(request):
    return render(
        request,
        "invoices/expense_category_list.html",
        {
            "active": "expense_categories",
            "categories": ExpenseCategory.objects.annotate(
                claim_count=Count("expenses"),
                spend=Coalesce(Sum("expenses__amount"), ZERO, output_field=MONEY),
            )
        }
    )


@login_required
def expense_category_edit(request, category_id=None):
    category = (
        get_object_or_404(ExpenseCategory, pk=category_id) if category_id else None
    )

    if request.method == "POST":
        form = ExpenseCategoryForm(request.POST, instance=category)

        if form.is_valid():
            saved = form.save()
            messages.success(request, f"Saved {saved.name}.")

            return redirect("expense_category_list")

    else:
        form = ExpenseCategoryForm(instance=category)

    return render(
        request,
        "invoices/simple_form.html",
        {
            "form": form,
            "heading": "Edit Category" if category else "New Expense Category",
            "cancel_url": reverse("expense_category_list"),
        }
    )


# ---------------------------------------------------------------- SAMPLING

@login_required
def sample_list(request):
    issues = SampleIssue.objects.select_related(
        "employee", "call_point", "created_by"
    ).prefetch_related("items__product", "items__batch")

    employee_id = request.GET.get("employee", "").strip()

    mine = field_employee(request.user)

    if mine is not None:
        # An MR login sees its own sample issues and nobody else's.
        issues = issues.filter(employee=mine)
    elif employee_id:
        issues = issues.filter(employee_id=employee_id)

    issues = list(issues)

    return render(
        request,
        "invoices/sample_list.html",
        {
            "active": "samples",
            "issues": issues,
            "employees": Employee.objects.filter(is_active=True),
            "selected_employee": employee_id,
            "total_units": sum(issue.total_units for issue in issues),
            "total_value": sum((issue.total_value for issue in issues), ZERO),
        }
    )


@login_required
def sample_create(request):
    """Hand samples to a doctor, taken straight out of sellable stock."""
    mine = field_employee(request.user)

    if request.method == "POST":
        form = SampleIssueForm(request.POST, employee=mine)

        batch_ids = request.POST.getlist("batch[]")
        quantities = post_column(request, "qty[]", len(batch_ids))

        lines, errors = _clean_sample_lines(batch_ids, quantities)

        if form.is_valid() and lines and not errors:
            issue_record = form.save(commit=False)
            issue_record.created_by = request.user
            issue_record.save()

            units = _issue_samples(issue_record, lines, request.user)

            messages.success(
                request,
                f"{issue_record.reference}: {units} sample(s) issued to "
                f"{issue_record.call_point or 'the field'}.",
            )

            return redirect("sample_list")

        for error in errors:
            messages.error(request, error)

        if not lines and not errors:
            messages.error(request, "Add at least one product line.")

    else:
        form = SampleIssueForm(
            initial={"date": timezone.localdate()}, employee=mine
        )

    return render(
        request,
        "invoices/sample_form.html",
        {
            "form": form,
            "me": mine,
            "products": Product.objects.filter(is_active=True),
        }
    )


def _clean_sample_lines(batch_ids, quantities):
    """Check every line against live stock before issuing any of it."""
    lines = []
    errors = []

    wanted = {}

    for index, raw_id in enumerate(batch_ids):
        if not raw_id:
            continue

        batch = Batch.objects.select_related("product").filter(pk=raw_id).first()

        if batch is None:
            errors.append(f"Row {index + 1}: unknown batch.")
            continue

        qty = safe_int(quantities[index])

        if qty <= 0:
            errors.append(f"Row {index + 1}: quantity must be at least 1.")
            continue

        if batch.is_expired:
            errors.append(
                f"{batch.product.name} batch {batch.batch_no} expired on "
                f"{batch.expiry_date:%d-%m-%Y} and cannot be sampled."
            )
            continue

        # Two rows can name the same batch; check the combined total.
        wanted[batch.pk] = wanted.get(batch.pk, 0) + qty

        if wanted[batch.pk] > batch.quantity:
            errors.append(
                f"{batch.product.name} batch {batch.batch_no}: only "
                f"{batch.quantity} in stock."
            )
            continue

        lines.append({"batch": batch, "qty": qty})

    return lines, errors


@transaction.atomic
def _issue_samples(issue_record, lines, user):
    units = 0

    for line in lines:
        batch = line["batch"]

        SampleIssueItem.objects.create(
            sample_issue=issue_record,
            product=batch.product,
            batch=batch,
            qty=line["qty"],
        )

        issue(
            batch,
            line["qty"],
            reference=issue_record.reference,
            note=(
                f"Sample to {issue_record.call_point}"
                if issue_record.call_point else "Sample issued"
            ),
            user=user,
            kind=StockMovement.SAMPLE,
        )

        units += line["qty"]

    return units


@login_required
def sample_report(request):
    """Samples by team member, by product and by doctor."""
    items = SampleIssueItem.objects.select_related(
        "product", "batch", "sample_issue__employee", "sample_issue__call_point"
    )

    by_employee = {}
    by_product = {}
    by_doctor = {}

    for line in items:
        issue_record = line.sample_issue

        who = issue_record.employee.full_name
        by_employee[who] = by_employee.get(who, 0) + line.qty

        by_product[line.product.name] = (
            by_product.get(line.product.name, 0) + line.qty
        )

        doctor = (
            issue_record.call_point.name if issue_record.call_point
            else "Not recorded"
        )
        by_doctor[doctor] = by_doctor.get(doctor, 0) + line.qty

    def ranked(mapping):
        return sorted(mapping.items(), key=lambda row: row[1], reverse=True)

    return render(
        request,
        "invoices/sample_report.html",
        {
            "active": "sample_report",
            "by_employee": ranked(by_employee),
            "by_product": ranked(by_product),
            "by_doctor": ranked(by_doctor)[:25],
            "total_units": sum(by_product.values()),
        }
    )


# ---------------------------------------------------------------- PAYROLL

@login_required
def payroll_list(request):
    runs = PayrollRun.objects.prefetch_related("payslips")

    return render(
        request,
        "invoices/payroll_list.html",
        {
            "active": "payroll",
            "runs": runs,
            "form": PayrollRunForm(initial={"month": timezone.localdate()}),
        }
    )


@login_required
def payroll_create(request):
    if request.method != "POST":
        return redirect("payroll_list")

    form = PayrollRunForm(request.POST)

    if not form.is_valid():
        for errors in form.errors.values():
            for error in errors:
                messages.error(request, error)

        return redirect("payroll_list")

    run = form.save(commit=False)
    run.created_by = request.user
    run.save()

    created = _generate_payslips(run)

    messages.success(
        request, f"{run}: generated {created} payslip(s)."
    )

    return redirect("payroll_detail", run_id=run.pk)


def month_range(month):
    """First and last day of the month `month` falls in."""
    start = month.replace(day=1)
    end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)

    return start, end


@transaction.atomic
def _generate_payslips(run):
    """One slip per active employee, with the month's expenses and commission."""
    created = 0

    start, end = month_range(run.month)

    for employee in Employee.objects.filter(is_active=True):
        reimbursement = (
            Expense.objects.filter(
                employee=employee,
                status=Expense.APPROVED,
                date__year=run.month.year,
                date__month=run.month.month,
            ).aggregate(t=Sum("amount"))["t"] or ZERO
        )

        # Salary-only staff carry a 0% rate, so this costs them nothing and
        # the slip simply leaves the commission lines off.
        sales = (
            employee.net_sales(start, end) if employee.earns_commission else ZERO
        )

        payslip = Payslip(
            run=run,
            employee=employee,
            basic_salary=employee.basic_salary,
            fuel_allowance=employee.fuel_allowance,
            mobile_allowance=employee.mobile_allowance,
            other_allowance=employee.other_allowance,
            expense_reimbursement=reimbursement,
            sales_amount=sales,
            commission_percent=employee.commission_percent,
        )

        payslip.recalculate_commission().recalculate().save()
        created += 1

    return created


@login_required
def payroll_detail(request, run_id):
    run = get_object_or_404(PayrollRun, pk=run_id)

    return render(
        request,
        "invoices/payroll_detail.html",
        {
            "run": run,
            "payslips": run.payslips.select_related("employee"),
        }
    )


@login_required
def payroll_finalise(request, run_id):
    run = get_object_or_404(PayrollRun, pk=run_id)

    run.status = PayrollRun.FINALISED
    run.save(update_fields=["status"])

    messages.success(request, f"{run} finalised.")

    return redirect("payroll_detail", run_id=run.pk)


@login_required
def payslip_edit(request, payslip_id):
    """Adjust deductions before the run is finalised."""
    payslip = get_object_or_404(
        Payslip.objects.select_related("run", "employee"), pk=payslip_id
    )

    if not payslip.run.is_editable:
        messages.error(request, "This payroll run has been finalised.")

        return redirect("payroll_detail", run_id=payslip.run_id)

    if request.method == "POST":
        for field in ("basic_salary", "fuel_allowance", "mobile_allowance",
                      "other_allowance", "expense_reimbursement",
                      "tax_deduction", "advance_deduction", "other_deduction"):
            setattr(
                payslip, field,
                safe_decimal(request.POST.get(field, "0"), max_value=MAX_TOTAL),
            )

        payslip.sales_amount = safe_decimal(
            request.POST.get("sales_amount", "0"), max_value=MAX_TOTAL
        )
        payslip.commission_percent = safe_decimal(
            request.POST.get("commission_percent", "0"), max_value=MAX_DISCOUNT
        )

        payslip.note = clip(request.POST.get("note", ""), 255)

        # Rate and sales are editable, the commission itself is not: it is
        # always the one times the other, so the slip cannot contradict itself.
        payslip.recalculate_commission().recalculate().save()

        messages.success(request, f"Updated {payslip.employee.full_name}'s slip.")

        return redirect("payroll_detail", run_id=payslip.run_id)

    return render(
        request,
        "invoices/payslip_form.html",
        {"payslip": payslip}
    )


@login_required
def payslip_pdf(request, payslip_id):
    payslip = get_object_or_404(
        Payslip.objects.select_related("run", "employee", "employee__territory"),
        pk=payslip_id,
    )

    refused = deny_unless_mine(request, payslip.employee)

    if refused is not None:
        return refused

    response = HttpResponse(
        render_payslip(payslip), content_type="application/pdf"
    )

    name = payslip.employee.employee_code or payslip.employee_id

    response["Content-Disposition"] = (
        f'attachment; filename="payslip-{name}-{payslip.run.month:%Y-%m}.pdf"'
    )

    return response


# ---------------------------------------------------------------- DAILY CALLS

@login_required
def daily_calls(request):
    """An MR's day: what was scheduled, and what has been reported so far."""
    day = parse_date(request.GET.get("date", "")) or timezone.localdate()

    employee_id = request.GET.get("employee", "").strip()
    employee = None

    mine = field_employee(request.user)

    if mine is not None:
        # An MR login cannot look at anyone else's day.
        employee = mine
    elif employee_id:
        employee = Employee.objects.filter(pk=employee_id).first()
    else:
        # Default to the team member linked to whoever is signed in.
        employee = Employee.objects.filter(user=request.user).first()

    scheduled = []
    reported = []

    if employee is not None:
        week_start = monday_of(day)
        weekday = day.weekday()

        plan = WeeklyPlan.objects.filter(
            employee=employee, week_start=week_start
        ).first()

        if plan is not None and weekday < 6:
            scheduled = list(
                plan.visits.filter(day=weekday)
                .select_related("call_point")
                .prefetch_related("report")
            )

        reported = list(
            CallReport.objects.filter(employee=employee, visit_date=day)
            .select_related("call_point", "sample_issue")
            .prefetch_related("products")
        )

    reported_visit_ids = {r.plan_visit_id for r in reported if r.plan_visit_id}

    return render(
        request,
        "invoices/daily_calls.html",
        {
            "active": "daily",
            "day": day,
            "previous_day": day - timedelta(days=1),
            "next_day": day + timedelta(days=1),
            "employee": employee,
            # A field login has nobody else's day to switch to, so the picker
            # is not offered rather than being offered and ignored.
            "employees": (
                [] if mine is not None
                else Employee.objects.filter(is_active=True)
            ),
            "locked_to_me": mine is not None,
            "scheduled": [
                {"visit": visit, "done": visit.pk in reported_visit_ids}
                for visit in scheduled
            ],
            "reported": reported,
            "unplanned": [r for r in reported if not r.was_planned],
            "samples_today": sum(r.samples_given for r in reported),
        }
    )


@login_required
def call_report_create(request, visit_id=None):
    """Log a visit, optionally against a scheduled slot."""
    plan_visit = (
        get_object_or_404(
            PlanVisit.objects.select_related("plan__employee", "call_point"),
            pk=visit_id,
        )
        if visit_id else None
    )

    if plan_visit is not None:
        refused = deny_unless_mine(request, plan_visit.plan.employee)

        if refused is not None:
            return refused

    # A field login is the person reporting, so the form neither asks who nor
    # offers doctors outside their own patch.
    mine = field_employee(request.user)

    if request.method == "POST":
        form = CallReportForm(request.POST, employee=mine)

        batch_ids = request.POST.getlist("batch[]")
        quantities = post_column(request, "qty[]", len(batch_ids))

        sample_lines, sample_errors = _clean_sample_lines(batch_ids, quantities)

        if form.is_valid() and not sample_errors:
            report = form.save(commit=False)
            report.plan_visit = plan_visit
            report.created_by = request.user
            report.save()
            form.save_m2m()

            if sample_lines:
                report.sample_issue = _samples_for_report(
                    report, sample_lines, request.user
                )
                report.save(update_fields=["sample_issue"])

            # A reported call closes its scheduled slot automatically.
            if plan_visit is not None:
                plan_visit.status = (
                    "done" if report.outcome == CallReport.MET else "missed"
                )
                plan_visit.remarks = report.feedback[:255]
                plan_visit.save(update_fields=["status", "remarks"])

            messages.success(
                request,
                f"Recorded visit to {report.call_point.name}"
                + (f" — {report.samples_given} sample(s) issued."
                   if report.samples_given else "."),
            )

            return redirect(
                f"{reverse('daily_calls')}?date={report.visit_date:%Y-%m-%d}"
                f"&employee={report.employee_id}"
            )

        for error in sample_errors:
            messages.error(request, error)

    else:
        initial = {"visit_date": timezone.localdate()}

        if plan_visit is not None:
            initial.update({
                "employee": plan_visit.plan.employee_id,
                "call_point": plan_visit.call_point_id,
                "visit_date": plan_visit.visit_date,
                "speciality": plan_visit.call_point.speciality,
            })

        form = CallReportForm(initial=initial, employee=mine)

    return render(
        request,
        "invoices/call_report_form.html",
        {
            "form": form,
            "plan_visit": plan_visit,
            "me": mine,
            "products": Product.objects.filter(is_active=True),
        }
    )


def _samples_for_report(report, lines, user):
    """Samples left on this call, issued from stock like any other sample."""
    issue_record = SampleIssue.objects.create(
        employee=report.employee,
        call_point=report.call_point,
        date=report.visit_date,
        note=f"Left on call with {report.doctor_name or report.call_point.name}",
        created_by=user,
    )

    _issue_samples(issue_record, lines, user)

    return issue_record


@login_required
def call_report_list(request):
    reports = CallReport.objects.select_related(
        "employee", "call_point", "sample_issue"
    ).prefetch_related("products")

    employee_id = request.GET.get("employee", "").strip()
    month = request.GET.get("month", "").strip()

    mine = field_employee(request.user)

    if mine is not None:
        reports = reports.filter(employee=mine)
    elif employee_id:
        reports = reports.filter(employee_id=employee_id)

    if month:
        parsed = parse_date(f"{month}-01")

        if parsed:
            reports = reports.filter(
                visit_date__year=parsed.year, visit_date__month=parsed.month
            )

    reports = list(reports)

    return render(
        request,
        "invoices/call_report_list.html",
        {
            "active": "calls",
            "reports": reports,
            "employees": Employee.objects.filter(is_active=True),
            "selected_employee": employee_id,
            "month": month,
            "met": len([r for r in reports if r.outcome == CallReport.MET]),
            "unplanned": len([r for r in reports if not r.was_planned]),
            "samples": sum(r.samples_given for r in reports),
        }
    )


@login_required
def call_report_summary(request):
    """Calls made versus calls planned, per team member."""
    month = request.GET.get("month", "").strip()

    reports = CallReport.objects.select_related("employee", "call_point")
    visits = PlanVisit.objects.select_related("plan__employee")

    parsed = parse_date(f"{month}-01") if month else None

    if parsed:
        reports = reports.filter(
            visit_date__year=parsed.year, visit_date__month=parsed.month
        )

    rows = {}

    for report in reports:
        row = rows.setdefault(
            report.employee.full_name,
            {"calls": 0, "met": 0, "unplanned": 0, "samples": 0, "doctors": set()},
        )

        row["calls"] += 1
        row["met"] += 1 if report.outcome == CallReport.MET else 0
        row["unplanned"] += 0 if report.was_planned else 1
        row["samples"] += report.samples_given
        row["doctors"].add(report.call_point_id)

    ranked = sorted(
        (
            {
                "name": name,
                "calls": data["calls"],
                "met": data["met"],
                "unplanned": data["unplanned"],
                "samples": data["samples"],
                "doctors": len(data["doctors"]),
                "met_percent": round(data["met"] * 100 / data["calls"])
                if data["calls"] else 0,
            }
            for name, data in rows.items()
        ),
        key=lambda row: row["calls"],
        reverse=True,
    )

    return render(
        request,
        "invoices/call_report_summary.html",
        {
            "active": "call_summary",
            "rows": ranked,
            "month": month,
            "total_calls": sum(row["calls"] for row in ranked),
        }
    )


# ------------------------------------------------------------------- ORDERS

@login_required
def order_list(request):
    """What the field has asked for, and what the office still owes them."""
    orders = Order.objects.select_related(
        "employee", "customer", "call_point", "invoice"
    ).prefetch_related("items__product")

    status_filter = request.GET.get("status", "").strip()
    employee_id = request.GET.get("employee", "").strip()

    mine = field_employee(request.user)

    if mine is not None:
        orders = orders.filter(employee=mine)
    elif employee_id:
        orders = orders.filter(employee_id=employee_id)

    if status_filter:
        orders = orders.filter(status=status_filter)
    elif mine is None:
        # The office's default view is the work still to do, not the archive.
        orders = orders.filter(status__in=[Order.PENDING, Order.APPROVED])

    orders = list(orders)

    return render(
        request,
        "invoices/order_list.html",
        {
            "active": "orders",
            "orders": orders,
            "employees": Employee.objects.filter(is_active=True),
            "statuses": Order.STATUS_CHOICES,
            "selected_status": status_filter,
            "selected_employee": employee_id,
            "locked_to_me": mine is not None,
            "value": sum((order.total for order in orders), ZERO),
            "waiting": Order.objects.filter(status=Order.PENDING).count(),
        }
    )


@login_required
def order_detail(request, order_id):
    order = get_object_or_404(
        Order.objects.select_related(
            "employee", "customer", "call_point", "invoice"
        ).prefetch_related("items__product"),
        pk=order_id,
    )

    refused = deny_unless_mine(request, order.employee)

    if refused is not None:
        return refused

    return render(
        request,
        "invoices/order_detail.html",
        {
            "active": "orders",
            "order": order,
            "can_act": not is_field_staff(request.user),
        }
    )


@login_required
def order_status(request, order_id, action):
    """Approve or reject an order without raising anything yet."""
    if is_field_staff(request.user):
        messages.error(request, "🚫 Only the office can approve orders.")

        return redirect("order_list")

    order = get_object_or_404(Order, pk=order_id)

    transitions = {
        "approve": Order.APPROVED,
        "reject": Order.REJECTED,
        "reopen": Order.PENDING,
    }

    if action not in transitions:
        messages.error(request, "Unknown action.")

        return redirect("order_detail", order_id=order.pk)

    if order.status == Order.INVOICED:
        messages.error(
            request,
            f"{order.order_no} has already been invoiced as "
            f"{order.invoice.invoice_no if order.invoice else 'an invoice'}.",
        )

        return redirect("order_detail", order_id=order.pk)

    order.status = transitions[action]
    order.reviewed_by = request.user
    order.reviewed_at = timezone.now()
    order.review_note = clip(request.POST.get("note", ""), 500)
    order.save()

    messages.success(request, f"{order.order_no} marked {order.get_status_display().lower()}.")

    return redirect("order_detail", order_id=order.pk)


# ------------------------------------------------------------------ TARGETS

@login_required
def target_list(request):
    """What each MR is expected to do this month, and how it is going."""
    month = request.GET.get("month", "").strip()
    parsed = parse_date(f"{month}-01") if month else None

    start, end = month_range(parsed or timezone.localdate())

    rows = []

    for target in Target.objects.filter(month=start).select_related("employee"):
        rows.append({"target": target, "achievement": target.achievement()})

    rows.sort(key=lambda row: row["target"].employee.full_name)

    # Anyone active without a target this month, so a gap is visible rather
    # than silently meaning "nothing expected".
    missing = Employee.objects.filter(is_active=True).exclude(
        pk__in=[row["target"].employee_id for row in rows]
    )

    return render(
        request,
        "invoices/target_list.html",
        {
            "active": "targets",
            "rows": rows,
            "missing": missing,
            "month": start.strftime("%Y-%m"),
            "start": start,
            "form": TargetForm(initial={"month": start.strftime("%Y-%m")}),
        }
    )


@login_required
def target_edit(request, target_id=None):
    target = get_object_or_404(Target, pk=target_id) if target_id else None

    if request.method == "POST":
        form = TargetForm(request.POST, instance=target)

        if form.is_valid():
            saved = form.save(commit=False)
            saved.set_by = request.user
            saved.save()

            _save_product_targets(request, saved)

            messages.success(
                request,
                f"Target set for {saved.employee.full_name}, "
                f"{saved.month:%B %Y}.",
            )

            return redirect(f"{reverse('target_list')}?month={saved.month:%Y-%m}")

        for errors in form.errors.values():
            for error in errors:
                messages.error(request, error)

    else:
        form = TargetForm(instance=target)

    # Paired up here rather than in the template: a template cannot look a
    # dict up by a variable key, and a product with no target needs to render
    # as an empty box rather than be missing.
    existing = {
        line.product_id: line.units
        for line in (target.product_targets.all() if target else [])
    }

    return render(
        request,
        "invoices/target_form.html",
        {
            "form": form,
            "target": target,
            "product_rows": [
                {"product": product, "units": existing.get(product.pk, "")}
                for product in Product.objects.filter(is_active=True)
            ],
        }
    )


def _save_product_targets(request, target):
    """Per-product unit targets, entered as one box per product.

    A blank or zero clears the line rather than storing a target of nothing.
    """
    for product in Product.objects.filter(is_active=True):
        units = safe_int(request.POST.get(f"units_{product.pk}", "0"))

        if units > 0:
            ProductTarget.objects.update_or_create(
                target=target, product=product, defaults={"units": units}
            )
        else:
            ProductTarget.objects.filter(
                target=target, product=product
            ).delete()


# ------------------------------------------------------------------ DOCTORS

@login_required
def doctor_list(request):
    """Every doctor, and where they currently sit."""
    doctors = Doctor.objects.select_related(
        "call_point", "call_point__territory"
    )

    query = request.GET.get("q", "").strip()
    territory_id = request.GET.get("territory", "").strip()

    mine = field_employee(request.user)

    if mine is not None and mine.territory_id is not None:
        doctors = doctors.filter(call_point__territory_id=mine.territory_id)
    elif territory_id:
        doctors = doctors.filter(call_point__territory_id=territory_id)

    if query:
        doctors = doctors.filter(
            Q(name__icontains=query)
            | Q(speciality__icontains=query)
            | Q(call_point__name__icontains=query)
        )

    return render(
        request,
        "invoices/doctor_list.html",
        {
            "active": "doctors",
            "doctors": doctors,
            "territories": Territory.objects.filter(is_active=True),
            "query": query,
            "selected_territory": territory_id,
            "recent_moves": DoctorMove.objects.select_related(
                "doctor", "from_call_point", "to_call_point"
            )[:15],
        }
    )


# ------------------------------------------------------------- THE MR PORTAL
#
# Everything below is what a field login sees. Each view resolves the signed-in
# user to one Employee and works only from that, so an MR cannot reach another
# MR's figures by editing a query string.

def _me(request):
    """The Employee behind this login, field staff or otherwise."""
    return (
        field_employee(request.user)
        or Employee.objects.filter(user=request.user).first()
    )


@login_required
def my_dashboard(request):
    """An MR's home page: today, this week, and the month's earnings."""
    me = _me(request)

    if me is None:
        return render(request, "invoices/my_dashboard.html", {"me": None})

    today = timezone.localdate()
    week_start = monday_of(today)
    month_start, month_end = month_range(today)

    plan = WeeklyPlan.objects.filter(
        employee=me, week_start=week_start
    ).first()

    today_planned = (
        plan.visits.filter(day=today.weekday()).count()
        if plan is not None and today.weekday() < 6 else 0
    )

    month_reports = CallReport.objects.filter(
        employee=me, visit_date__gte=month_start, visit_date__lte=month_end
    )

    sales = me.net_sales(month_start, month_end)

    return render(
        request,
        "invoices/my_dashboard.html",
        {
            "active": "my_dashboard",
            "me": me,
            "today": today,
            "plan": plan,
            "today_planned": today_planned,
            "today_done": CallReport.objects.filter(
                employee=me, visit_date=today
            ).count(),
            "month_calls": month_reports.count(),
            "month_met": month_reports.filter(outcome=CallReport.MET).count(),
            "sales": sales,
            "commission": me.commission_on(month_start, month_end),
            "month_start": month_start,
            "pending_expenses": Expense.objects.filter(
                employee=me, status=Expense.PENDING
            ).count(),
        }
    )


@login_required
def my_plan(request):
    """This week's schedule, laid out day by day."""
    me = _me(request)

    week_start = parse_date(request.GET.get("week", "")) or timezone.localdate()
    week_start = monday_of(week_start)

    plan = (
        WeeklyPlan.objects.filter(employee=me, week_start=week_start).first()
        if me is not None else None
    )

    days = plan_days(plan)

    return render(
        request,
        "invoices/my_plan.html",
        {
            "active": "my_plan",
            "me": me,
            "plan": plan,
            "days": days,
            "made_total": sum(day["made_count"] for day in days),
            "samples_total": sum(day["samples"] for day in days),
            "week_start": week_start,
            "previous_week": week_start - timedelta(days=7),
            "next_week": week_start + timedelta(days=7),
            "today": timezone.localdate(),
        }
    )


@login_required
def my_sales(request):
    """What an MR sold this month, and what the percentage comes to.

    Deliberately itemised: an MR who can see every invoice behind the figure
    does not have to take payroll's word for it.
    """
    me = _me(request)

    month = request.GET.get("month", "").strip()
    parsed = parse_date(f"{month}-01") if month else None

    start, end = month_range(parsed or timezone.localdate())

    invoices = []
    returns = []

    if me is not None:
        invoices = list(
            Invoice.objects.filter(sales_rep=me, date__gte=start, date__lte=end)
            .select_related("customer")
        )
        returns = list(
            SalesReturn.objects.filter(
                invoice__sales_rep=me, date__gte=start, date__lte=end
            ).select_related("invoice", "customer")
        )

    invoiced = sum((i.total for i in invoices), ZERO)
    credited = sum((r.total for r in returns), ZERO)
    net = invoiced - credited

    rate = me.commission_percent if me is not None else ZERO

    return render(
        request,
        "invoices/my_sales.html",
        {
            "active": "my_sales",
            "me": me,
            "invoices": invoices,
            "returns": returns,
            "invoiced": invoiced,
            "credited": credited,
            "net": net,
            "rate": rate,
            "commission": (net * rate / Decimal("100")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            ),
            "month": start.strftime("%Y-%m"),
            "start": start,
            "end": end,
        }
    )


@login_required
def my_payslips(request):
    """Payslips already issued to this employee."""
    me = _me(request)

    payslips = (
        Payslip.objects.filter(employee=me).select_related("run")
        if me is not None else Payslip.objects.none()
    )

    return render(
        request,
        "invoices/my_payslips.html",
        {
            "active": "my_payslips",
            "me": me,
            "payslips": payslips,
        }
    )


@login_required
def commission_report(request):
    """Every earner's commission for a month, for whoever signs off payroll."""
    month = request.GET.get("month", "").strip()
    parsed = parse_date(f"{month}-01") if month else None

    start, end = month_range(parsed or timezone.localdate())

    rows = []

    for employee in Employee.objects.filter(is_active=True):
        sales = employee.net_sales(start, end)

        if not sales and not employee.earns_commission:
            continue

        rows.append({
            "employee": employee,
            "sales": sales,
            "rate": employee.commission_percent,
            "commission": employee.commission_on(start, end),
            "invoices": Invoice.objects.filter(
                sales_rep=employee, date__gte=start, date__lte=end
            ).count(),
        })

    rows.sort(key=lambda row: row["sales"], reverse=True)

    unattributed = Invoice.objects.filter(
        sales_rep__isnull=True, date__gte=start, date__lte=end
    )

    return render(
        request,
        "invoices/commission_report.html",
        {
            "active": "commission_report",
            "rows": rows,
            "month": start.strftime("%Y-%m"),
            "start": start,
            "end": end,
            "total_sales": sum((row["sales"] for row in rows), ZERO),
            "total_commission": sum((row["commission"] for row in rows), ZERO),
            "unattributed": unattributed.count(),
            "unattributed_value": (
                unattributed.aggregate(t=Sum("total"))["t"] or ZERO
            ),
        }
    )


@login_required
def employee_login_setup(request, employee_id):
    """Give a team member their own login, or reset the password on it.

    Field logins are deliberately created here rather than in Django's admin:
    the role and the Employee link have to be set together, and a login with
    only one of the two either sees everything or sees nothing.
    """
    if is_field_staff(request.user):
        messages.error(request, "🚫 Only the office can manage logins.")

        return redirect("my_dashboard")

    employee = get_object_or_404(Employee, pk=employee_id)

    if request.method == "POST":
        username = clip(request.POST.get("username", "").strip(), 150)
        password = request.POST.get("password", "")

        if len(password) < 8:
            messages.error(request, "Password must be at least 8 characters.")

            return redirect("employee_login", employee_id=employee.pk)

        if employee.user is not None:
            account = employee.user
        else:
            if not username:
                messages.error(request, "Pick a username for this login.")

                return redirect("employee_login", employee_id=employee.pk)

            if User.objects.filter(username=username).exists():
                messages.error(request, f"The username “{username}” is taken.")

                return redirect("employee_login", employee_id=employee.pk)

            account = User.objects.create_user(username=username)

            employee.user = account
            employee.save(update_fields=["user"])

        account.set_password(password)
        account.first_name = employee.full_name[:150]
        account.email = employee.email
        account.save()

        role, _ = UserRolls.objects.get_or_create(user=account)
        role.role = UserRolls.ROLE_FIELD
        role.phone = employee.phone
        role.save()

        messages.success(
            request,
            f"{employee.full_name} can now sign in as “{account.username}”.",
        )

        return redirect("team_list")

    suggested = ""

    if employee.user is None:
        base = (employee.employee_code or employee.full_name).lower()
        suggested = "".join(c for c in base if c.isalnum() or c in "-_.")[:150]

    return render(
        request,
        "invoices/employee_login.html",
        {
            "employee": employee,
            "suggested": suggested,
        }
    )


# ---------------------------------------------------------------- OWNERSHIP

@login_required
def partner_list(request):
    """Who owns the business, and where each partner's account stands."""
    partners = list(Partner.objects.all())

    net_profit = finance.profit_and_loss()["net_profit"]

    rows = [
        {
            "partner": partner,
            "invested": partner.invested,
            "drawn": partner.drawn,
            "net_contributed": partner.net_contributed,
            "profit_share": partner.share_of(net_profit),
            "capital_balance": (
                partner.net_contributed + partner.share_of(net_profit)
            ),
        }
        for partner in partners
    ]

    return render(
        request,
        "invoices/partner_list.html",
        {
            "active": "partners",
            "rows": rows,
            "net_profit": net_profit,
            "fair_shares": finance.capital_fair_shares(),
            "funding": finance.funding_needed(),
            "total_share": Partner.total_share(),
            # Shown rather than silently corrected: shares that do not make
            # 100 mean somebody's profit is unassigned, and only the owners
            # can say whose it is.
            "balanced": Partner.shares_are_balanced(),
            "totals": {
                "invested": sum((r["invested"] for r in rows), ZERO),
                "drawn": sum((r["drawn"] for r in rows), ZERO),
                "net_contributed": sum((r["net_contributed"] for r in rows), ZERO),
                "profit_share": sum((r["profit_share"] for r in rows), ZERO),
                "capital_balance": sum((r["capital_balance"] for r in rows), ZERO),
            },
        }
    )


@login_required
def partner_edit(request, partner_id=None):
    partner = get_object_or_404(Partner, pk=partner_id) if partner_id else None

    if request.method == "POST":
        form = PartnerForm(request.POST, instance=partner)

        if form.is_valid():
            saved = form.save()
            messages.success(request, f"Saved {saved.full_name}.")

            return redirect("partner_list")

    else:
        form = PartnerForm(instance=partner)

    return render(
        request,
        "invoices/partner_form.html",
        {
            "active": "partners",
            "form": form,
            "partner": partner,
        }
    )


@login_required
def partner_statement(request, partner_id):
    """One partner's capital account, entry by entry."""
    partner = get_object_or_404(Partner, pk=partner_id)

    statement = finance.partner_statement(partner)

    # This partner's row out of the fair-share table, so the statement can say
    # whether he is square with his brothers without recomputing it here.
    standing = next(
        (
            row for row in finance.capital_fair_shares()["rows"]
            if row["partner"].pk == partner.pk
        ),
        None,
    )

    running = ZERO
    entries = []

    for transaction in statement["transactions"]:
        running += transaction.signed_amount

        entries.append({"transaction": transaction, "balance": running})

    return render(
        request,
        "invoices/partner_statement.html",
        {
            "active": "partners",
            "statement": statement,
            "partner": partner,
            "entries": entries,
            "standing": standing,
        }
    )


@login_required
def capital_record(request):
    """Record money a partner put in or took out."""
    partner_id = request.GET.get("partner", "").strip()

    if request.method == "POST":
        form = CapitalTransactionForm(request.POST)

        if form.is_valid():
            entry = form.save(commit=False)
            entry.recorded_by = request.user
            entry.save()

            messages.success(
                request,
                f"Recorded {entry.get_kind_display().lower()} of "
                f"{entry.amount:.2f} for {entry.partner.full_name}.",
            )

            return redirect("partner_statement", partner_id=entry.partner_id)

    else:
        form = CapitalTransactionForm(
            initial={
                "date": timezone.localdate(),
                "partner": partner_id or None,
            }
        )

    return render(
        request,
        "invoices/capital_form.html",
        {
            "active": "partners",
            "form": form,
        }
    )


@login_required
def profit_report(request):
    """The trading result, and how it divides between the partners."""
    start = parse_date(request.GET.get("start", ""))
    end = parse_date(request.GET.get("end", ""))

    split = finance.distribution(start, end)

    return render(
        request,
        "invoices/profit_report.html",
        {
            "active": "profit_report",
            "split": split,
            "pnl": split["profit_and_loss"],
            "position": finance.where_the_money_is(),
            "start": request.GET.get("start", ""),
            "end": request.GET.get("end", ""),
        }
    )


@login_required
def assessment_report(request):
    """Profit and loss quarter by quarter, half by half, or year by year."""
    kind = request.GET.get("period", finance.QUARTER)

    years = finance.trading_years()

    year = safe_int(request.GET.get("year", "")) or timezone.localdate().year

    # A year with nothing in it would render an empty grid with no
    # explanation, so fall back to one there is something to show for.
    if year not in years:
        year = years[0]

    return render(
        request,
        "invoices/assessment_report.html",
        {
            "active": "assessment",
            "report": finance.assessment(kind, year),
            "period_choices": finance.PERIOD_CHOICES,
            "years": years,
            "selected_period": kind,
            "selected_year": year,
        }
    )


@login_required
def sales_report(request):
    """How the business is selling: day by day, and what is driving it."""
    start = parse_date(request.GET.get("start", ""))
    end = parse_date(request.GET.get("end", ""))

    # Defaults to the last 30 days rather than all time: this page is for
    # reading the current rhythm, and a year of history flattens it.
    if start is None and end is None:
        end = timezone.localdate()
        start = end - timedelta(days=29)

    return render(
        request,
        "invoices/sales_report.html",
        {
            "active": "sales_report",
            "rows": finance.daily_sales(start, end),
            "summary": finance.selling_days(start, end),
            "pnl": finance.profit_and_loss(start, end),
            "products": finance.top_products(start, end),
            "customers": finance.top_customers(start, end, limit=10),
            "reps": finance.sales_by_rep(start, end),
            "start": start.isoformat() if start else "",
            "end": end.isoformat() if end else "",
        }
    )


@login_required
def ageing_report(request):
    """Who owes, and how long they have owed it for."""
    ageing = finance.receivables_ageing()

    return render(
        request,
        "invoices/ageing_report.html",
        {
            "active": "ageing",
            "ageing": ageing,
            "oldest": finance.oldest_debts(),
        }
    )
