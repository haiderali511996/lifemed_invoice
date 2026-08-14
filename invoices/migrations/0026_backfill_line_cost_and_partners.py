"""Give existing sales their cost, and put the four partners on the books.

Two jobs, both one-off.

**Line costs.** Invoice lines have only just started recording what their
stock cost. Everything sold before this reads zero, which would show those
sales as pure profit. The batch each line came from still knows its cost
price, so that is copied across - the best figure available, and the same one
the reports would have used anyway. Lines never picked from stock genuinely
have no cost and are left at zero.

**Partners.** The business is owned in equal quarters by four brothers. They
are seeded here rather than typed in afterwards so the ownership records are
right from the first time anyone opens the page. Only if the table is empty:
re-running this must not duplicate anyone, and must never overwrite shares
that have since been changed on purpose.
"""

from decimal import Decimal

from django.db import migrations

ZERO = Decimal("0.00")

# Equal quarters, which is how the company was set up.
FOUNDING_PARTNERS = [
    "Mustafa Ali",
    "Mujtaba Ali",
    "Muhabbat Ali",
    "Haider Ali",
]

EQUAL_SHARE = (Decimal("100") / len(FOUNDING_PARTNERS)).quantize(Decimal("0.01"))


def backfill_line_costs(apps, schema_editor):
    Item = apps.get_model("invoices", "Item")
    SalesReturnItem = apps.get_model("invoices", "SalesReturnItem")

    for item in Item.objects.filter(unit_cost=ZERO).select_related("stock_batch"):
        if item.stock_batch_id is None:
            continue

        item.unit_cost = item.stock_batch.cost_price
        item.save(update_fields=["unit_cost"])

    # Credit notes take their cost from the line they reversed, so a return
    # always cancels exactly what its sale charged.
    for returned in SalesReturnItem.objects.filter(unit_cost=ZERO).select_related(
        "item", "batch"
    ):
        if returned.item_id is not None:
            returned.unit_cost = returned.item.unit_cost
        elif returned.batch_id is not None:
            returned.unit_cost = returned.batch.cost_price
        else:
            continue

        returned.save(update_fields=["unit_cost"])


def clear_line_costs(apps, schema_editor):
    apps.get_model("invoices", "Item").objects.update(unit_cost=ZERO)
    apps.get_model("invoices", "SalesReturnItem").objects.update(unit_cost=ZERO)


def seed_partners(apps, schema_editor):
    Partner = apps.get_model("invoices", "Partner")

    if Partner.objects.exists():
        return

    for name in FOUNDING_PARTNERS:
        Partner.objects.create(
            full_name=name,
            share_percent=EQUAL_SHARE,
            is_active=True,
        )


def unseed_partners(apps, schema_editor):
    Partner = apps.get_model("invoices", "Partner")

    # Only the untouched seed rows: a partner who has money recorded against
    # them is real history and is left alone.
    Partner.objects.filter(
        full_name__in=FOUNDING_PARTNERS, capital_transactions__isnull=True
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("invoices", "0025_partner_capital_and_line_cost"),
    ]

    operations = [
        migrations.RunPython(backfill_line_costs, clear_line_costs),
        migrations.RunPython(seed_partners, unseed_partners),
    ]
