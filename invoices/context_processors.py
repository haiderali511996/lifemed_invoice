"""Values the base layout needs on every page.

The sidebar shows an overdue badge and the header shows the signed-in user's
avatar and role, so every view would otherwise have to supply them.
"""

from .models import UserRolls, is_super_admin


def erp_shell(request):
    user = getattr(request, "user", None)

    if user is None or not user.is_authenticated:
        return {}

    profile = UserRolls.objects.filter(user=user).first()

    if profile is None:
        # Users created before the role signal existed, or via raw SQL.
        profile = UserRolls.objects.create(user=user)

    # Imported lazily: this module is loaded for every request, and the ledger
    # helpers pull in aggregate machinery that the login page has no use for.
    from .views import overdue_invoices

    return {
        "user_profile": profile,
        "is_super_admin": is_super_admin(user),
        "overdue_count": overdue_invoices().count(),
        "search_query": request.GET.get("q", ""),
    }
