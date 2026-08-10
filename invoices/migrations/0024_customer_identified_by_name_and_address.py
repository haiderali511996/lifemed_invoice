"""Let one name cover more than one customer.

A pharmacy chain runs branches under a single name, and each branch is its
own account with its own deliveries and its own balance. The unique
constraint on the name forced them to share one record, so invoicing a
second branch overwrote the first branch's address.

Dropping the constraint cannot introduce duplicates into existing data - it
was in force until now - and the pair (name, address) is checked in
`Customer.at_address` instead, since MySQL will not index a TEXT column
without a prefix length.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("invoices", "0023_allocate_existing_payments"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="customer",
            options={"ordering": ["name", "id"]},
        ),
        migrations.AlterField(
            model_name="customer",
            name="name",
            field=models.CharField(max_length=255),
        ),
    ]
