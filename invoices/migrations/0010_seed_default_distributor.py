"""Turn the single hardcoded template into the first Distributor record.

Invoicing used to assume one company: template.pdf in the project root and an
"HHC-" prefix baked into the code. This registers that company as a normal
Distributor so nothing is special-cased, copies its template into media, and
attaches every existing invoice to it.
"""

import os
import shutil

from django.conf import settings
from django.db import migrations

LEGACY_NAME = "HADI HEALTH CARE"
LEGACY_CODE = "HHC"
LEGACY_TEMPLATE = "invoice_templates/template.pdf"


def seed(apps, schema_editor):
    Distributor = apps.get_model("invoices", "Distributor")
    Invoice = apps.get_model("invoices", "Invoice")

    if Distributor.objects.exists():
        return

    source = os.path.join(settings.BASE_DIR, "template.pdf")
    stored = ""

    if os.path.exists(source):
        destination = os.path.join(settings.MEDIA_ROOT, LEGACY_TEMPLATE)
        os.makedirs(os.path.dirname(destination), exist_ok=True)

        if not os.path.exists(destination):
            shutil.copyfile(source, destination)

        stored = LEGACY_TEMPLATE

    # Continue the existing series rather than restarting it.
    highest = 0

    for value in Invoice.objects.values_list("invoice_no", flat=True):
        suffix = str(value).rsplit("-", 1)[-1]

        if suffix.isdigit():
            highest = max(highest, int(suffix))

    start = highest + 1 if highest else int(
        os.getenv("INVOICE_START_NUMBER", "9965")
    )

    distributor = Distributor.objects.create(
        name=LEGACY_NAME,
        code=LEGACY_CODE,
        template=stored,
        invoice_start_number=start,
        is_active=True,
        is_default=True,
    )

    Invoice.objects.filter(distributor__isnull=True).update(distributor=distributor)


def unseed(apps, schema_editor):
    Distributor = apps.get_model("invoices", "Distributor")
    Invoice = apps.get_model("invoices", "Invoice")

    Invoice.objects.update(distributor=None)
    Distributor.objects.filter(code=LEGACY_CODE).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("invoices", "0009_batch_distributor_product_purchase_supplier_and_more"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
