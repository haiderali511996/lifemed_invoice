"""What the business earned, what it is holding, and whose share is whose.

Every figure here is derived from records that already exist - invoices,
credit notes, purchases, expenses, payslips - rather than typed in again.
Nothing in this module writes anything.

Three things it is careful about, because partners divide the result:

- **An expense reimbursed through payroll is one cost, not two.** Approved
  employee expenses are copied onto that month's payslip, so counting the
  expense and the whole payslip would charge the business twice for it.
- **Goods that came back but were not restocked are still gone.** A credit
  note reverses the sale; only a return that went back on the shelf reverses
  its cost too.
- **A draft payroll run is not a cost yet.** It can still be regenerated, so
  only finalised runs count - and the number of drafts left out is reported
  rather than being quietly dropped.
"""

from decimal import Decimal

from django.db.models import DecimalField, ExpressionWrapper, F, Sum

from .models import (
    Batch,
    CapitalTransaction,
    Expense,
    Invoice,
    Item,
    Partner,
    Payment,
    PayrollRun,
    Payslip,
    PurchaseItem,
    SalesReturn,
    SalesReturnItem,
    SampleIssueItem,
    ZERO,
)

MONEY = DecimalField(max_digits=16, decimal_places=2)

PENNY = Decimal("0.01")


def money(value):
    return (value or ZERO).quantize(PENNY)


def _between(queryset, field, start, end):
    """Narrow to a date range. Either end may be left open."""
    if start is not None:
        queryset = queryset.filter(**{f"{field}__gte": start})

    if end is not None:
        queryset = queryset.filter(**{f"{field}__lte": end})

    return queryset


def _sum(queryset, field):
    return queryset.aggregate(t=Sum(field))["t"] or ZERO


def _sum_cost(queryset):
    """Total of qty x unit_cost over lines that carry a snapshotted cost."""
    return queryset.aggregate(
        t=Sum(
            ExpressionWrapper(F("qty") * F("unit_cost"), output_field=MONEY)
        )
    )["t"] or ZERO


# ------------------------------------------------------------------- trading

def revenue(start=None, end=None):
    """Sales actually billed, less anything credited back."""
    invoiced = _sum(_between(Invoice.objects.all(), "date", start, end), "total")
    credited = _sum(
        _between(SalesReturn.objects.all(), "date", start, end), "total"
    )

    return invoiced - credited


def cost_of_goods_sold(start=None, end=None):
    """What the goods behind those sales cost us.

    Returns reverse their cost only when the goods were restocked. Damaged or
    expired stock is credited to the customer and thrown away, so the business
    carries both the lost sale and the lost goods - which is what happened.
    """
    sold = _sum_cost(
        _between(Item.objects.all(), "invoice__date", start, end)
    )

    restocked = _sum_cost(
        _between(
            SalesReturnItem.objects.filter(sales_return__restock=True),
            "sales_return__date",
            start,
            end,
        )
    )

    return sold - restocked


def uncosted_sales(start=None, end=None):
    """Invoice lines sold without a cost against them.

    A line typed in by hand rather than picked from stock moves no stock and
    carries no cost, so its whole value lands in profit. Small numbers are
    normal; a large one means the gross margin below is flattering.
    """
    lines = _between(
        Item.objects.filter(unit_cost=ZERO), "invoice__date", start, end
    )

    value = lines.aggregate(
        t=Sum(
            ExpressionWrapper(
                F("qty") * F("price") * (Decimal("100") - F("discount"))
                / Decimal("100"),
                output_field=MONEY,
            )
        )
    )["t"] or ZERO

    return {"count": lines.count(), "value": money(value)}


def samples_cost(start=None, end=None):
    """Free goods handed to doctors, at what they cost us.

    Valued at the batch's cost price now rather than a snapshot taken on the
    day, so re-receiving a batch number at a different price shifts this line
    slightly. It is small next to the rest, and no sale depends on it.
    """
    return (
        _between(SampleIssueItem.objects.all(), "sample_issue__date", start, end)
        .aggregate(
            t=Sum(
                ExpressionWrapper(
                    F("qty") * F("batch__cost_price"), output_field=MONEY
                )
            )
        )["t"] or ZERO
    )


def operating_expenses(start=None, end=None):
    """Claims and company costs. Rejected ones never left the bank."""
    return _sum(
        _between(
            Expense.objects.exclude(status=Expense.REJECTED),
            "date",
            start,
            end,
        ),
        "amount",
    )


def employment_cost(start=None, end=None):
    """Wages, allowances and commission - but not reimbursed expenses.

    Those are already counted under operating expenses; a payslip is only
    the vehicle that pays them out. Subtracting them here is what stops one
    fuel claim being charged to the partners twice.
    """
    payslips = _between(
        Payslip.objects.filter(run__status=PayrollRun.FINALISED),
        "run__month",
        start,
        end,
    )

    return _sum(payslips, "gross_pay") - _sum(payslips, "expense_reimbursement")


def draft_payroll_runs(start=None, end=None):
    """Runs left out of the figures above because they are not committed."""
    return _between(
        PayrollRun.objects.filter(status=PayrollRun.DRAFT), "month", start, end
    ).count()


def profit_and_loss(start=None, end=None):
    """The whole trading picture for a period, or for all time."""
    sales = revenue(start, end)
    cogs = cost_of_goods_sold(start, end)
    gross = sales - cogs

    samples = samples_cost(start, end)
    expenses = operating_expenses(start, end)
    wages = employment_cost(start, end)

    net = gross - samples - expenses - wages

    return {
        "revenue": money(sales),
        "cost_of_goods_sold": money(cogs),
        "gross_profit": money(gross),
        "gross_margin": (
            money(gross * Decimal("100") / sales) if sales else ZERO
        ),
        "samples": money(samples),
        "expenses": money(expenses),
        "wages": money(wages),
        "total_costs": money(samples + expenses + wages),
        "net_profit": money(net),
        "net_margin": money(net * Decimal("100") / sales) if sales else ZERO,
        "uncosted": uncosted_sales(start, end),
        "draft_payroll": draft_payroll_runs(start, end),
    }


# ------------------------------------------------------------------- capital

def stock_at_cost():
    """What is sitting on the shelves, valued at what we paid for it."""
    return (
        Batch.objects.filter(quantity__gt=0)
        .aggregate(
            t=Sum(
                ExpressionWrapper(
                    F("quantity") * F("cost_price"), output_field=MONEY
                )
            )
        )["t"] or ZERO
    )


def receivables():
    """Billed and not yet collected, net of credit notes."""
    return (
        _sum(Invoice.objects.all(), "total")
        - _sum(Payment.objects.all(), "amount")
        - _sum(SalesReturn.objects.all(), "total")
    )


def purchases_total(start=None, end=None):
    """Goods bought from suppliers, at cost."""
    return (
        _between(PurchaseItem.objects.all(), "purchase__date", start, end)
        .aggregate(
            t=Sum(
                ExpressionWrapper(
                    F("quantity") * F("cost_price"), output_field=MONEY
                )
            )
        )["t"] or ZERO
    )


def capital_introduced():
    return (
        CapitalTransaction.objects.filter(kind=CapitalTransaction.INVESTMENT)
        .aggregate(t=Sum("amount"))["t"] or ZERO
    )


def capital_withdrawn():
    return (
        CapitalTransaction.objects.filter(kind=CapitalTransaction.DRAWING)
        .aggregate(t=Sum("amount"))["t"] or ZERO
    )


def where_the_money_is():
    """Partners' funds set against what the business is actually holding.

    The two sides do not meet, and the gap is the point: this system records
    stock and what customers owe, but has no bank account and does not track
    what is still owed to suppliers. The difference is therefore cash in hand
    and bank, less unpaid supplier bills - a real figure, just not one that
    can be read from here. It is shown rather than hidden so nobody reads the
    two columns as a balance sheet that failed to balance.
    """
    stock = stock_at_cost()
    owed_to_us = receivables()
    held = stock + owed_to_us

    introduced = capital_introduced()
    withdrawn = capital_withdrawn()
    retained = profit_and_loss()["net_profit"]

    funds = introduced - withdrawn + retained

    return {
        "stock_at_cost": money(stock),
        "receivables": money(owed_to_us),
        "tracked_assets": money(held),
        "capital_introduced": money(introduced),
        "capital_withdrawn": money(withdrawn),
        "retained_profit": money(retained),
        "partners_funds": money(funds),
        "unaccounted": money(held - funds),
        "purchases_total": money(purchases_total()),
    }


# ------------------------------------------------------------------ partners

def distribution(start=None, end=None):
    """Each partner's slice of the profit, and whether the slices add up.

    Rounding each share to the paisa can leave the shares adding to a hair
    less or more than the profit itself. That remainder is returned rather
    than buried, because a statement that does not reconcile to the last
    paisa is the one nobody trusts.
    """
    result = profit_and_loss(start, end)
    net = result["net_profit"]

    partners = list(Partner.objects.filter(is_active=True))

    rows = [
        {
            "partner": partner,
            "share_percent": partner.share_percent,
            "profit_share": partner.share_of(net),
        }
        for partner in partners
    ]

    allocated = sum((row["profit_share"] for row in rows), ZERO)

    return {
        "profit_and_loss": result,
        "net_profit": net,
        "rows": rows,
        "allocated": money(allocated),
        "rounding": money(net - allocated),
        "total_share": Partner.total_share(),
        "balanced": Partner.shares_are_balanced(),
    }


def partner_statement(partner, start=None, end=None):
    """One partner's capital account: what they put in, took out, and earned."""
    net = profit_and_loss(start, end)["net_profit"]

    transactions = _between(
        partner.capital_transactions.all(), "date", start, end
    ).order_by("date", "id")

    invested = partner.invested
    drawn = partner.drawn
    profit_share = partner.share_of(net)

    return {
        "partner": partner,
        "transactions": transactions,
        "invested": money(invested),
        "drawn": money(drawn),
        "net_contributed": money(invested - drawn),
        "profit_share": money(profit_share),
        "capital_balance": money(invested - drawn + profit_share),
        "company_net_profit": net,
    }
