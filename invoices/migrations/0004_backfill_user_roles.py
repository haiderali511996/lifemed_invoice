"""Move super-admin access from a hardcoded username onto the UserRolls role.

Views used to compare `request.user.username` against a literal string. This
gives every existing user a UserRolls row and promotes the account that the
old code treated as super admin, so nobody loses access on deploy.
"""

from django.db import migrations

LEGACY_SUPER_ADMIN_USERNAME = "novamax_super_secure2200"


def backfill_roles(apps, schema_editor):
    User = apps.get_model("auth", "User")
    UserRolls = apps.get_model("invoices", "UserRolls")

    for user in User.objects.all():
        role = (
            "super_admin"
            if user.username == LEGACY_SUPER_ADMIN_USERNAME or user.is_superuser
            else "manager"
        )

        obj, created = UserRolls.objects.get_or_create(
            user=user, defaults={"role": role}
        )

        if not created and role == "super_admin" and obj.role != role:
            obj.role = role
            obj.save(update_fields=["role"])


def noop(apps, schema_editor):
    """Roles are data, not schema - nothing to undo."""


class Migration(migrations.Migration):

    dependencies = [
        ("invoices", "0003_userrolls_invoicelog"),
    ]

    operations = [
        migrations.RunPython(backfill_roles, noop),
    ]
