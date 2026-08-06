from django.shortcuts import render, redirect, get_object_or_404
from django.http import FileResponse, JsonResponse
from django.contrib.auth.decorators import login_required
from django.contrib.auth import authenticate, login, logout
from django.utils.timezone import now
from django.contrib import messages

from .models import Customer, Invoice, Item, InvoiceLog, UserRolls, is_super_admin

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

    return JsonResponse({
        "customer_name": customer.name,
        "address": customer.address or "",
        "ntn": customer.ntn or "",
        "sales_tax": customer.sales_tax or "",
        "license_no": customer.license_no or "",
        "last_invoice_no": invoice.invoice_no if invoice else None,
        "items": items,
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