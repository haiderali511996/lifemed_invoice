"""Stock movements.

Every change to stock goes through here so the batch quantity and the movement
ledger can never disagree. Quantities are updated with an atomic F() expression
rather than read-modify-write, which would lose one of two concurrent sales.
"""

from django.db import transaction
from django.db.models import F

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
