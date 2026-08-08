"""Remove test invoices, and optionally the customer, from a live database.

Deleting an invoice by hand in the Django admin leaves stock wrong: lines that
were picked from stock deducted a batch, and nothing puts that back. This
reverses the stock first, then deletes, inside one transaction.

Nothing is deleted without --confirm; the default run only reports.

    python manage.py delete_invoice_data --customer 1
    python manage.py delete_invoice_data --customer 1 --confirm
    python manage.py delete_invoice_data --invoice HHC-9965 --confirm
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import F

from invoices.models import (
    Batch, Customer, Invoice, InvoiceLog, Item, Payment, StockMovement,
)


class Command(BaseCommand):
    help = "Delete invoices (and optionally a customer), returning any stock."

    def add_arguments(self, parser):
        parser.add_argument(
            "--customer", type=int,
            help="Customer ID, as in the /ledgers/<id>/ URL.",
        )
        parser.add_argument(
            "--invoice", action="append", default=[],
            help="Invoice number, e.g. HHC-9965. Repeatable.",
        )
        parser.add_argument(
            "--confirm", action="store_true",
            help="Actually delete. Without it, nothing is written.",
        )
        parser.add_argument(
            "--keep-customer", action="store_true",
            help="Delete the invoices but keep the customer record.",
        )

    def handle(self, *args, **options):
        customer_id = options["customer"]
        numbers = options["invoice"]

        if not customer_id and not numbers:
            raise CommandError("Give --customer and/or --invoice.")

        customer = None

        if customer_id:
            customer = Customer.objects.filter(pk=customer_id).first()

            if customer is None:
                raise CommandError(f"No customer with ID {customer_id}.")

        invoices = self._invoices(customer, numbers)

        if not invoices and customer is None:
            raise CommandError("Nothing matched.")

        self._report(customer, invoices, options["keep_customer"])

        if not options["confirm"]:
            self.stdout.write(self.style.WARNING(
                "\nNothing deleted. Re-run with --confirm to apply."
            ))
            return

        with transaction.atomic():
            returned = self._return_stock(invoices)
            counts = self._delete(customer, invoices, options["keep_customer"])

        self.stdout.write(self.style.SUCCESS(
            f"\nDeleted {counts['invoices']} invoice(s), {counts['items']} line(s), "
            f"{counts['payments']} payment(s), {counts['logs']} log entry(ies). "
            f"Returned {returned} unit(s) to stock."
            + (" Customer deleted." if counts["customer"] else "")
        ))

    # ---------------------------------------------------------------- helpers

    def _invoices(self, customer, numbers):
        query = Invoice.objects.none()

        if customer is not None:
            query = Invoice.objects.filter(customer=customer)

        if numbers:
            by_number = Invoice.objects.filter(invoice_no__in=numbers)

            missing = set(numbers) - set(
                by_number.values_list("invoice_no", flat=True)
            )

            if missing:
                raise CommandError(
                    "No such invoice(s): " + ", ".join(sorted(missing))
                )

            query = query | by_number

        return list(query.distinct().select_related("customer"))

    def _report(self, customer, invoices, keep_customer):
        if customer is not None:
            self.stdout.write(
                f"Customer: {customer.name} (ID {customer.pk})"
            )
            self.stdout.write(
                f"  outstanding balance: {customer.outstanding_balance}"
            )

        self.stdout.write(f"\nInvoices to delete: {len(invoices)}")

        for invoice in invoices:
            self.stdout.write(
                f"  {invoice.invoice_no}  {invoice.date}  "
                f"total {invoice.total}  ({invoice.items.count()} line(s))"
            )

            for item in invoice.items.select_related("stock_batch"):
                if item.stock_batch_id:
                    self.stdout.write(
                        f"      returns {item.qty} to "
                        f"{item.stock_batch.product.name} / "
                        f"{item.stock_batch.batch_no}"
                    )

        payments = self._payments(customer, invoices)

        self.stdout.write(f"\nPayments to delete: {payments.count()}")

        for payment in payments:
            self.stdout.write(f"  {payment.paid_on}  {payment.amount}")

        if customer is not None and not keep_customer:
            self.stdout.write(
                self.style.WARNING("\nThe customer record will also be deleted.")
            )

    def _payments(self, customer, invoices):
        if customer is not None:
            return Payment.objects.filter(customer=customer)

        return Payment.objects.filter(invoice__in=invoices)

    def _return_stock(self, invoices):
        """Put back everything these invoices took out of stock."""
        returned = 0

        items = Item.objects.filter(
            invoice__in=invoices, stock_batch__isnull=False
        ).select_related("stock_batch", "product")

        for item in items:
            if item.qty <= 0:
                continue

            Batch.objects.filter(pk=item.stock_batch_id).update(
                quantity=F("quantity") + item.qty
            )

            StockMovement.objects.create(
                product_id=item.product_id or item.stock_batch.product_id,
                batch_id=item.stock_batch_id,
                quantity=item.qty,
                kind=StockMovement.ADJUSTMENT,
                reference=item.invoice.invoice_no,
                note=f"Invoice {item.invoice.invoice_no} deleted",
            )

            returned += item.qty

        return returned

    def _delete(self, customer, invoices, keep_customer):
        counts = {
            "invoices": len(invoices),
            "items": Item.objects.filter(invoice__in=invoices).count(),
            "payments": self._payments(customer, invoices).count(),
            "logs": InvoiceLog.objects.filter(invoice__in=invoices).count(),
            "customer": False,
        }

        self._payments(customer, invoices).delete()

        # Cascades to items and log entries.
        Invoice.objects.filter(pk__in=[i.pk for i in invoices]).delete()

        if customer is not None and not keep_customer:
            customer.delete()
            counts["customer"] = True

        return counts
