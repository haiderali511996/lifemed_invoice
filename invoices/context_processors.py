"""Values the base layout needs on every page.

The sidebar shows an overdue badge and the header shows the signed-in user's
avatar and role, so every view would otherwise have to supply them.
"""

from .models import Order, UserRolls, is_field_staff, is_super_admin


def erp_shell(request):
    user = getattr(request, "user", None)

    if user is None or not user.is_authenticated:
        return {}

    profile = UserRolls.objects.filter(user=user).first()

    if profile is None:
        # Users created before the role signal existed, or via raw SQL.
        profile = UserRolls.objects.create(user=user)

    field = is_field_staff(user)

    # Imported lazily: this module is loaded for every request, and the ledger
    # helpers pull in aggregate machinery that the login page has no use for.
    from .views import overdue_invoices

    return {
        "user_profile": profile,
        "is_super_admin": is_super_admin(user),
        "is_field_staff": field,
        # Receivables are the office's business, so an MR is neither shown the
        # badge nor charged the query that builds it.
        "overdue_count": 0 if field else overdue_invoices().count(),
        # Orders waiting on the office. An MR sees their own menu without a
        # badge - the number is the office's queue, not theirs.
        "pending_orders": (
            0 if field
            else Order.objects.filter(status=Order.PENDING).count()
        ),
        "search_query": request.GET.get("q", ""),
    }
