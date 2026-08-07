"""Seed the expense categories a pharma marketing company actually uses."""

from django.db import migrations

CATEGORIES = [
    ("Fuel Allowance", "FUEL", True),
    ("Doctor Refreshment", "REFRESH", True),
    ("Literature Expense", "LIT", True),
    ("Promotional Material", "PROMO", True),
    ("DRAP Fees", "DRAP", False),
    ("Travel & Lodging", "TRAVEL", True),
    ("Mobile & Internet", "MOBILE", True),
    ("Office & Utilities", "OFFICE", False),
    ("Salary & Payroll", "SALARY", False),
    ("Miscellaneous", "MISC", True),
]


def seed(apps, schema_editor):
    ExpenseCategory = apps.get_model("invoices", "ExpenseCategory")

    for name, code, per_employee in CATEGORIES:
        ExpenseCategory.objects.get_or_create(
            name=name,
            defaults={"code": code, "per_employee": per_employee},
        )


def unseed(apps, schema_editor):
    ExpenseCategory = apps.get_model("invoices", "ExpenseCategory")

    # Only remove the untouched seeds; anything with spend against it stays.
    for name, _, _ in CATEGORIES:
        category = ExpenseCategory.objects.filter(name=name).first()

        if category and not category.expenses.exists():
            category.delete()


class Migration(migrations.Migration):

    dependencies = [
        ("invoices", "0014_expensecategory_payrollrun_sampleissue_and_more"),
    ]

    operations = [migrations.RunPython(seed, unseed)]
