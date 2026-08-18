"""Stock movements.

Every change to stock goes through here so the batch quantity and the movement
ledger can never disagree. Quantities are updated with an atomic F() expression
rather than read-modify-write, which would lose one of two concurrent sales.
"""

from django.db import transaction
from django.db.models import F, Sum

from .models import Batch, StockMovement


class StockError(Exception):
    """A stock operation that must not silently proceed."""


@transaction.atomic
def receive(batch, quantity, *, reference="", note="", user=None,
            kind=StockMovement.PURCHASE):
    """Add stock to a batch and record the movement."""
    quantity = int(quantity)

    if quantity <= 0:
        raise StockError("Received quantity must be greater than zero.")

    Batch.objects.filter(pk=batch.pk).update(
        quantity=F("quantity") + quantity,
        received_quantity=F("received_quantity") + quantity,
    )

    movement = StockMovement.objects.create(
        product=batch.product,
        batch=batch,
        quantity=quantity,
        kind=kind,
        reference=reference,
        note=note,
        created_by=user,
    )

    batch.refresh_from_db()

    return movement


@transaction.atomic
def issue(batch, quantity, *, reference="", note="", user=None,
          kind=StockMovement.SALE, allow_negative=False):
    """Remove stock from a batch.

    Refuses to go negative unless explicitly allowed - overselling a pharma
    batch means shipping stock that does not exist.
    """
    quantity = int(quantity)

    if quantity <= 0:
        raise StockError("Issued quantity must be greater than zero.")

    # Re-read under the row lock: the caller's copy may be stale.
    locked = Batch.objects.select_for_update().get(pk=batch.pk)

    if not allow_negative and locked.quantity < quantity:
        raise StockError(
            f"Only {locked.quantity} left of {locked.product.name} "
            f"batch {locked.batch_no}; cannot issue {quantity}."
        )

    Batch.objects.filter(pk=batch.pk).update(quantity=F("quantity") - quantity)

    movement = StockMovement.objects.create(
        product=locked.product,
        batch=locked,
        quantity=-quantity,
        kind=kind,
        reference=reference,
        note=note,
        created_by=user,
    )

    batch.refresh_from_db()

    return movement


@transaction.atomic
def adjust(batch, new_quantity, *, note="", user=None):
    """Set a batch to a counted quantity, recording the difference."""
    new_quantity = int(new_quantity)

    if new_quantity < 0:
        raise StockError("Counted quantity cannot be negative.")

    locked = Batch.objects.select_for_update().get(pk=batch.pk)
    difference = new_quantity - locked.quantity

    if difference == 0:
        return None

    Batch.objects.filter(pk=batch.pk).update(quantity=new_quantity)

    movement = StockMovement.objects.create(
        product=locked.product,
        batch=locked,
        quantity=difference,
        kind=StockMovement.ADJUSTMENT,
        note=note or "Stock count",
        created_by=user,
    )

    batch.refresh_from_db()

    return movement


def allocate_fefo(product, quantity):
    """Pick batches First-Expired-First-Out.

    Returns [(batch, quantity), ...] covering as much as stock allows, and the
    shortfall. Expired batches are never allocated.
    """
    from django.utils import timezone

    remaining = int(quantity)
    picks = []

    batches = (
        product.batches.filter(
            quantity__gt=0, expiry_date__gte=timezone.localdate()
        )
        .order_by("expiry_date", "id")
    )

    for batch in batches:
        if remaining <= 0:
            break

        take = min(batch.quantity, remaining)
        picks.append((batch, take))
        remaining -= take

    return picks, remaining


@transaction.atomic
def record_sales_return(sales_return, lines, user=None):
    """Credit an invoice and, unless the goods are unsaleable, restock them.

    `lines` is [{item, qty, batch}]. Returns (created_count, restocked_units).
    """
    from .models import SalesReturnItem
    from decimal import Decimal

    created = 0
    restocked = 0
    total = Decimal("0.00")

    for line in lines:
        item = line["item"]
        qty = int(line["qty"])

        if qty <= 0:
            continue

        batch = line.get("batch") or item.stock_batch

        # Free packs come back in the same proportion as the billed ones.
        # Worked out on the running total rather than this return alone, so
        # ten returned in two lots of five brings back both free packs -
        # rounding each half on its own would lose one of them.
        bonus = 0

        if item.bonus and item.qty:
            already = SalesReturnItem.objects.filter(item=item).aggregate(
                q=Sum("qty"), b=Sum("bonus")
            )

            returned_before = already["q"] or 0
            bonus_before = already["b"] or 0

            # Rounded down, so a return never puts back more free packs than
            # went out with the goods.
            bonus = (
                (returned_before + qty) * item.bonus // item.qty
            ) - bonus_before

            bonus = max(0, bonus)

        returned = SalesReturnItem.objects.create(
            sales_return=sales_return,
            item=item,
            name=item.name,
            qty=qty,
            price=item.price,
            discount=item.discount,
            batch=batch if sales_return.restock else None,
            bonus=bonus,
            # The cost these goods went out at, so putting them back reverses
            # exactly what the sale charged rather than today's cost price.
            unit_cost=item.unit_cost,
        )

        total += returned.line_total
        created += 1

        # Damaged or expired goods are credited but never put back on the
        # shelf, so the customer is made whole without corrupting stock.
        if sales_return.restock and batch is not None:
            receive(
                batch,
                qty + bonus,
                reference=sales_return.return_no,
                note=f"Return against {sales_return.invoice.invoice_no}",
                user=user,
                kind=StockMovement.RETURN,
            )

            restocked += qty + bonus

    sales_return.total = total
    sales_return.save(update_fields=["total"])

    return created, restocked
