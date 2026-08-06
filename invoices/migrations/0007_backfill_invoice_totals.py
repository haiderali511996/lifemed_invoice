"""Recover totals for invoices created before Invoice.total existed.

generate_invoice already recorded the net payable in InvoiceLog, so the
earliest log row for each invoice is the authoritative figure. Without this,
every historic invoice would show a zero balance in the ledger.
"""

from django.db import migrations


def backfill_invoice_totals(apps, schema_editor):
    Invoice = apps.get_model("invoices", "Invoice")
    InvoiceLog = apps.get_model("invoices", "InvoiceLog")

    totals = {}

    for log in InvoiceLog.objects.order_by("id").iterator():
        totals.setdefault(log.invoice_id, log.amount)

    for invoice in Invoice.objects.iterator():
        amount = totals.get(invoice.id)

        if amount:
            invoice.total = amount
            invoice.save(update_fields=["total"])


def noop(apps, schema_editor):
    """Totals are derived data - nothing to undo."""


class Migration(migrations.Migration):

    dependencies = [
        ("invoices", "0006_alter_invoice_options_invoice_total_userrolls_avatar_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill_invoice_totals, noop),
    ]
