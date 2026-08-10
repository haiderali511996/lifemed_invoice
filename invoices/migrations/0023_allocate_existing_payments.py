"""Apply every payment already taken to the invoices it pays for.

Until now a payment reduced an invoice's balance only when it named that
invoice. Money taken "against the account" - which is what happens when a
customer hands over a lump sum - reduced the customer's ledger and nothing
else, so the ledger read settled while every invoice still read due.

This walks the existing payments in the order they were received and applies
each one the way `Payment.allocate` now does: the invoice it names first, then
the oldest outstanding bills. It writes allocations only; no payment, invoice
or total is altered, so the money in the system is exactly what it was.
"""

from decimal import Decimal

from django.db import migrations

ZERO = Decimal("0.00")


def allocate_existing(apps, schema_editor):
    Payment = apps.get_model("invoices", "Payment")
    Invoice = apps.get_model("invoices", "Invoice")
    SalesReturn = apps.get_model("invoices", "SalesReturn")
    PaymentAllocation = apps.get_model("invoices", "PaymentAllocation")

    # Historical models carry no properties, so what each invoice still owes
    # has to be tracked here as the payments are walked through.
    owed = {}

    for invoice in Invoice.objects.all():
        credited = sum(
            (
                credit_note.total
                for credit_note in SalesReturn.objects.filter(invoice=invoice)
            ),
            ZERO,
        )

        owed[invoice.pk] = invoice.total - credited

    # Oldest first, so the allocations land the way they would have if the
    # payments had been recorded against invoices at the time.
    payments = Payment.objects.order_by("paid_on", "id").select_related(
        "customer", "invoice"
    )

    allocations = []

    for payment in payments:
        remaining = payment.amount

        targets = []

        if payment.invoice_id is not None:
            targets.append(payment.invoice_id)

        targets.extend(
            Invoice.objects.filter(customer_id=payment.customer_id)
            .exclude(pk=payment.invoice_id)
            .order_by("date", "id")
            .values_list("pk", flat=True)
        )

        for invoice_id in targets:
            if remaining <= ZERO:
                break

            still_owed = owed.get(invoice_id, ZERO)

            if still_owed <= ZERO:
                continue

            applied = min(remaining, still_owed)

            allocations.append(
                PaymentAllocation(
                    payment_id=payment.pk, invoice_id=invoice_id, amount=applied
                )
            )

            owed[invoice_id] = still_owed - applied
            remaining -= applied

    PaymentAllocation.objects.bulk_create(allocations)


def clear_allocations(apps, schema_editor):
    apps.get_model("invoices", "PaymentAllocation").objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("invoices", "0022_paymentallocation"),
    ]

    operations = [
        migrations.RunPython(allocate_existing, clear_allocations),
    ]
