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

from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import (
    Count, DecimalField, ExpressionWrapper, F, OuterRef, Subquery, Sum,
)
from django.db.models.functions import Coalesce

from .models import (
    Account,
    Batch,
    CapitalTransaction,
    Customer,
    Expense,
    Invoice,
    Item,
    Partner,
    Payment,
    PaymentAllocation,
    PayrollRun,
    Payslip,
    PurchaseItem,
    SalesReturn,
    SalesReturnItem,
    SampleIssueItem,
    StockMovement,
    Supplier,
    SupplierPayment,
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



def stock_adjustments(start=None, end=None):
    """Stock written off, or found, outside a sale or a purchase.

    Binning expired goods is a real cost and never appeared as one: the
    inventory dropped and the profit did not, so the partners were shown a
    profit that included stock already in the skip. Negative here is a
    write-off, positive is stock found.
    """
    return (
        _between(
            StockMovement.objects.filter(kind=StockMovement.ADJUSTMENT),
            "created_at__date", start, end,
        )
        .aggregate(
            t=Sum(
                ExpressionWrapper(
                    F("quantity") * F("batch__cost_price"), output_field=MONEY
                )
            )
        )["t"] or ZERO
    )


def profit_and_loss(start=None, end=None):
    """The whole trading picture for a period, or for all time."""
    sales = revenue(start, end)
    cogs = cost_of_goods_sold(start, end)
    gross = sales - cogs

    samples = samples_cost(start, end)
    expenses = operating_expenses(start, end)
    wages = employment_cost(start, end)

    # A write-off is negative, so subtracting it adds the loss to the costs.
    adjustments = stock_adjustments(start, end)

    net = gross - samples - expenses - wages + adjustments

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
        "stock_adjustments": money(adjustments),
        "stock_written_off": money(-adjustments if adjustments < ZERO else ZERO),
        "total_costs": money(samples + expenses + wages - adjustments),
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


def collections(start=None, end=None):
    """Cash taken from customers."""
    return _sum(_between(Payment.objects.all(), "paid_on", start, end), "amount")


def capital_fair_shares():
    """Whether each partner has actually funded the share he owns.

    Partners who own the business in equal quarters are expected to have paid
    for it in equal quarters. One who has put in less than his share is being
    carried by his brothers - their money is funding stock and wages that his
    quarter of the profit is earned on. That difference is a debt to the
    company, and this is where it is named.

    Measured on net capital, so a partner who takes a drawing back out falls
    behind by exactly what he took - which is what has happened to the money.
    """
    partners = list(Partner.objects.filter(is_active=True))

    contributed = [(partner, partner.net_contributed) for partner in partners]
    total = sum((amount for _, amount in contributed), ZERO)

    rows = []

    for partner, actual in contributed:
        expected = (
            total * partner.share_percent / Decimal("100")
        ).quantize(PENNY)

        rows.append({
            "partner": partner,
            "share_percent": partner.share_percent,
            "expected": expected,
            "actual": money(actual),
            # Positive: owes the company. Negative: has funded more than his
            # share, so the company is carrying his money.
            "owed": money(expected - actual),
        })

    owed = sum((row["owed"] for row in rows if row["owed"] > ZERO), ZERO)

    return {
        "rows": rows,
        "total_capital": money(total),
        "total_owed": money(owed),
        "square": all(row["owed"] == ZERO for row in rows),
    }


def funding_needed(start=None, end=None):
    """What the business has spent that the partners have to fund.

    Buying stock, paying the team and settling expenses all take money out;
    what customers pay brings it back in. The difference is what the partners
    have had to cover between them, and their shares of it are what each one
    is expected to bring.

    Purchases are counted as though the supplier was paid on receipt. Money
    still owed to suppliers is not recorded anywhere in this system, so a
    business buying on credit needs less cash than this suggests.
    """
    bought = purchases_total(start, end)
    spent = operating_expenses(start, end)
    paid_out = employment_cost(start, end)
    taken = collections(start, end)

    outgoings = bought + spent + paid_out
    shortfall = outgoings - taken

    introduced = capital_introduced() - capital_withdrawn()

    # Each partner's slice of the bill. Charged on what is left after the
    # customers have paid, not on the gross spend: money the business has
    # already collected pays for itself, and asking the partners for it
    # again would put in more than the business ever needed.
    rows = []

    for partner in Partner.objects.filter(is_active=True):
        due = (
            shortfall * partner.share_percent / Decimal("100")
        ).quantize(PENNY)

        paid = partner.net_contributed

        rows.append({
            "partner": partner,
            "share_percent": partner.share_percent,
            "share_of_spend": (
                outgoings * partner.share_percent / Decimal("100")
            ).quantize(PENNY),
            "should_pay": due,
            "already_paid": money(paid),
            "still_to_pay": money(due - paid),
        })

    return {
        "purchases": money(bought),
        "expenses": money(spent),
        "wages": money(paid_out),
        "outgoings": money(outgoings),
        "collections": money(taken),
        "shortfall": money(shortfall),
        "capital_in": money(introduced),
        # More than the partners have put in means the business is funding
        # itself out of what it collects; less means it still needs them.
        "still_to_fund": money(shortfall - introduced),
        "rows": rows,
        "total_still_to_pay": money(
            sum((row["still_to_pay"] for row in rows if row["still_to_pay"] > ZERO), ZERO)
        ),
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


# --------------------------------------------------------------- assessment

QUARTER = "quarter"
HALF = "half"
YEAR = "year"

PERIOD_CHOICES = (
    (QUARTER, "Quarterly"),
    (HALF, "Half-yearly"),
    (YEAR, "Yearly"),
)

# How many periods of each kind fit in a year, and how many months each runs.
PERIOD_SHAPE = {
    QUARTER: (4, 3),
    HALF: (2, 6),
    YEAR: (1, 12),
}


def period_range(kind, year, index):
    """First and last day of the index-th quarter, half or year."""
    _, months = PERIOD_SHAPE[kind]

    first_month = (index - 1) * months + 1
    last_month = first_month + months - 1

    return (
        date(year, first_month, 1),
        date(year, last_month, monthrange(year, last_month)[1]),
    )


def period_label(kind, year, index):
    if kind == YEAR:
        return str(year)

    return f"{'Q' if kind == QUARTER else 'H'}{index} {year}"


def periods_in(kind, year):
    """Every period of this kind in one year, oldest first."""
    count, _ = PERIOD_SHAPE[kind]

    return [
        (period_label(kind, year, index), *period_range(kind, year, index))
        for index in range(1, count + 1)
    ]


def assessment(kind, year):
    """The trading result period by period, with each partner's share.

    Partners want to see whether the business is improving, not only what it
    has made since it started - so this reports each quarter, half or year on
    its own rather than as a running total.

    A period's profit is worked out from what traded inside it. Capital and
    drawings are left out on purpose: they move a partner's own money, not
    the business's result.
    """
    if kind not in PERIOD_SHAPE:
        kind = QUARTER

    partners = list(Partner.objects.filter(is_active=True))

    rows = []

    for label, start, end in periods_in(kind, year):
        result = profit_and_loss(start, end)

        rows.append({
            "label": label,
            "start": start,
            "end": end,
            "pnl": result,
            "shares": [
                {
                    "partner": partner,
                    "amount": partner.share_of(result["net_profit"]),
                }
                for partner in partners
            ],
        })

    totals = {
        field: money(sum((row["pnl"][field] for row in rows), ZERO))
        for field in (
            "revenue", "cost_of_goods_sold", "gross_profit",
            "wages", "expenses", "samples", "net_profit",
        )
    }

    return {
        "kind": kind,
        "year": year,
        "rows": rows,
        "partners": partners,
        "totals": totals,
        "partner_totals": [
            {
                "partner": partner,
                "amount": money(
                    sum(
                        (
                            row["shares"][position]["amount"]
                            for row in rows
                        ),
                        ZERO,
                    )
                ),
            }
            for position, partner in enumerate(partners)
        ],
    }


def trading_years():
    """Years there is anything to report on, newest first.

    Taken from the invoices actually raised, so the year picker never offers
    a year with nothing in it - and always offers this one.
    """
    years = set(
        Invoice.objects.dates("date", "year").values_list("date__year", flat=True)
    )

    years.add(date.today().year)

    return sorted(years, reverse=True)


# ------------------------------------------------------------- selling rhythm

def _net_line_value():
    """Value of a sold line after its own discount."""
    return ExpressionWrapper(
        F("qty") * F("price") * (Decimal("100") - F("discount")) / Decimal("100"),
        output_field=MONEY,
    )


def daily_sales(start=None, end=None):
    """What was sold on each trading day, with the cost against it.

    Only days that actually traded appear. A distributor closed on Sunday
    should not read a row of zeroes as a bad day.
    """
    days = {}

    def row_for(day):
        return days.setdefault(day, {
            "date": day, "invoices": 0,
            "invoiced": ZERO, "credited": ZERO, "cost": ZERO,
        })

    for entry in (
        _between(Invoice.objects.all(), "date", start, end)
        .values("date")
        .annotate(total=Sum("total"), count=Count("id"))
    ):
        row = row_for(entry["date"])
        row["invoiced"] = entry["total"] or ZERO
        row["invoices"] = entry["count"]

    for entry in (
        _between(SalesReturn.objects.all(), "date", start, end)
        .values("date")
        .annotate(total=Sum("total"))
    ):
        row_for(entry["date"])["credited"] = entry["total"] or ZERO

    for entry in (
        _between(Item.objects.all(), "invoice__date", start, end)
        .values("invoice__date")
        .annotate(
            total=Sum(
                ExpressionWrapper(
                    F("qty") * F("unit_cost"), output_field=MONEY
                )
            )
        )
    ):
        row_for(entry["invoice__date"])["cost"] = entry["total"] or ZERO

    for entry in (
        _between(
            SalesReturnItem.objects.filter(sales_return__restock=True),
            "sales_return__date", start, end,
        )
        .values("sales_return__date")
        .annotate(
            total=Sum(
                ExpressionWrapper(
                    F("qty") * F("unit_cost"), output_field=MONEY
                )
            )
        )
    ):
        row_for(entry["sales_return__date"])["cost"] -= entry["total"] or ZERO

    rows = []

    for day in sorted(days):
        row = days[day]
        sold = row["invoiced"] - row["credited"]
        gross = sold - row["cost"]

        rows.append({
            "date": day,
            "invoices": row["invoices"],
            "revenue": money(sold),
            "cost": money(row["cost"]),
            "gross_profit": money(gross),
            "margin": money(gross * Decimal("100") / sold) if sold else ZERO,
        })

    return rows


def selling_days(start=None, end=None):
    """How many distinct days actually traded, and the average taken on one."""
    rows = daily_sales(start, end)

    total = sum((row["revenue"] for row in rows), ZERO)
    invoices = sum(row["invoices"] for row in rows)

    return {
        "days": len(rows),
        "invoices": invoices,
        "revenue": money(total),
        "per_day": money(total / len(rows)) if rows else ZERO,
        "per_invoice": money(total / invoices) if invoices else ZERO,
        "best": max(rows, key=lambda row: row["revenue"]) if rows else None,
    }


def top_products(start=None, end=None, limit=20):
    """What is actually moving, by value and by the profit it earned.

    Net of returns: a product sold and sent straight back has not moved.
    """
    sold = (
        _between(
            Item.objects.filter(product__isnull=False), "invoice__date", start, end
        )
        .values("product_id", "product__name", "product__code")
        .annotate(
            units=Sum("qty"),
            revenue=Sum(_net_line_value()),
            cost=Sum(
                ExpressionWrapper(
                    F("qty") * F("unit_cost"), output_field=MONEY
                )
            ),
        )
    )

    returned = {}

    for entry in (
        _between(
            SalesReturnItem.objects.filter(item__product__isnull=False),
            "sales_return__date", start, end,
        )
        .values("item__product_id")
        .annotate(
            units=Sum("qty"),
            revenue=Sum(
                ExpressionWrapper(
                    F("qty") * F("price")
                    * (Decimal("100") - F("discount")) / Decimal("100"),
                    output_field=MONEY,
                )
            ),
        )
    ):
        returned[entry["item__product_id"]] = entry

    rows = []

    for entry in sold:
        back = returned.get(entry["product_id"], {})

        revenue = (entry["revenue"] or ZERO) - (back.get("revenue") or ZERO)
        cost = entry["cost"] or ZERO
        units = (entry["units"] or 0) - (back.get("units") or 0)

        rows.append({
            "product_id": entry["product_id"],
            "name": entry["product__name"],
            "code": entry["product__code"],
            "units": units,
            "revenue": money(revenue),
            "cost": money(cost),
            "gross_profit": money(revenue - cost),
            "margin": (
                money((revenue - cost) * Decimal("100") / revenue)
                if revenue else ZERO
            ),
        })

    rows.sort(key=lambda row: row["revenue"], reverse=True)

    return rows[:limit]


def top_customers(start=None, end=None, limit=20):
    """Who is buying, net of what they sent back."""
    credited = dict(
        _between(SalesReturn.objects.all(), "date", start, end)
        .values_list("customer_id")
        .annotate(total=Sum("total"))
    )

    rows = []

    for entry in (
        _between(Invoice.objects.all(), "date", start, end)
        .values("customer_id", "customer__name", "customer__address")
        .annotate(total=Sum("total"), count=Count("id"))
    ):
        revenue = (entry["total"] or ZERO) - (
            credited.get(entry["customer_id"]) or ZERO
        )

        rows.append({
            "customer_id": entry["customer_id"],
            "name": entry["customer__name"],
            "address": entry["customer__address"],
            "invoices": entry["count"],
            "revenue": money(revenue),
        })

    rows.sort(key=lambda row: row["revenue"], reverse=True)

    return rows[:limit]


def sales_by_rep(start=None, end=None):
    """What each MR sold. Invoices with nobody credited are kept separate."""
    rows = []

    for entry in (
        _between(Invoice.objects.all(), "date", start, end)
        .values("sales_rep_id", "sales_rep__full_name")
        .annotate(total=Sum("total"), count=Count("id"))
    ):
        rows.append({
            "name": entry["sales_rep__full_name"] or "Not credited to anyone",
            "credited": entry["sales_rep_id"] is not None,
            "invoices": entry["count"],
            "revenue": money(entry["total"] or ZERO),
        })

    rows.sort(key=lambda row: row["revenue"], reverse=True)

    return rows


def expiry_exposure(within_days=90):
    """Stock about to die, in money rather than in batch counts.

    A count says three batches; it does not say whether that is a rounding
    error or a month's profit. Only the money tells you whether to discount
    it and move it.
    """
    today = date.today()
    horizon = today + timedelta(days=within_days)

    live = Batch.objects.filter(quantity__gt=0)

    def valued(queryset):
        return queryset.aggregate(
            t=Sum(
                ExpressionWrapper(
                    F("quantity") * F("cost_price"), output_field=MONEY
                )
            )
        )["t"] or ZERO

    expired = live.filter(expiry_date__lt=today)
    expiring = live.filter(expiry_date__gte=today, expiry_date__lte=horizon)

    return {
        "expired_value": money(valued(expired)),
        "expired_count": expired.count(),
        "expiring_value": money(valued(expiring)),
        "expiring_count": expiring.count(),
        "within_days": within_days,
    }


def month_to_date(today=None):
    """This month so far, and the same run of days last month.

    Compared against the same number of days rather than the whole of last
    month, so a comparison made on the 3rd is not measured against a full
    month it was never going to beat.
    """
    today = today or date.today()

    start = today.replace(day=1)
    elapsed = (today - start).days

    previous_end = start - timedelta(days=1)
    previous_start = previous_end.replace(day=1)

    return {
        "this_month": profit_and_loss(start, today),
        "last_month": profit_and_loss(
            previous_start,
            min(previous_start + timedelta(days=elapsed), previous_end),
        ),
        "today": profit_and_loss(today, today),
        "start": start,
        "days_elapsed": elapsed + 1,
    }


# --------------------------------------------------------------------- ageing

# How old a debt is, counted from the day it was invoiced. No due date is
# recorded anywhere, so the invoice date is the only honest starting point.
AGEING_BUCKETS = (
    ("current", "Up to 30 days", 0, 30),
    ("d31_60", "31 - 60 days", 31, 60),
    ("d61_90", "61 - 90 days", 61, 90),
    ("d90_plus", "Over 90 days", 91, None),
)


def _outstanding_invoices():
    """Invoices with money still on them, and how much.

    The paid and credited figures come from subqueries rather than two
    aggregates in one query: joined that way they multiply each other out,
    and Sum(distinct=True) is not a fix because it drops genuinely repeated
    amounts.
    """
    paid = Subquery(
        PaymentAllocation.objects.filter(invoice=OuterRef("pk"))
        .values("invoice")
        .annotate(total=Sum("amount"))
        .values("total"),
        output_field=MONEY,
    )

    credited = Subquery(
        SalesReturn.objects.filter(invoice=OuterRef("pk"))
        .values("invoice")
        .annotate(total=Sum("total"))
        .values("total"),
        output_field=MONEY,
    )

    return (
        Invoice.objects.select_related("customer")
        .annotate(
            settled=Coalesce(paid, ZERO, output_field=MONEY),
            returned=Coalesce(credited, ZERO, output_field=MONEY),
        )
        .annotate(
            owing=ExpressionWrapper(
                F("total") - F("settled") - F("returned"), output_field=MONEY
            )
        )
        .filter(owing__gt=ZERO)
        .order_by("date", "id")
    )


def receivables_ageing(as_at=None):
    """What is owed, sorted by how long it has been owed for.

    A single "outstanding" figure says how much; it does not say how worried
    to be. Money a week old is a sale, money four months old is a problem,
    and only splitting them apart tells you which pharmacy to ring first.
    """
    as_at = as_at or date.today()

    customers = {}
    totals = {key: ZERO for key, *_ in AGEING_BUCKETS}

    for invoice in _outstanding_invoices():
        age = (as_at - invoice.date).days

        for key, _, lower, upper in AGEING_BUCKETS:
            if age >= lower and (upper is None or age <= upper):
                bucket = key
                break
        else:                                   # pragma: no cover - unreachable
            bucket = "d90_plus"

        row = customers.setdefault(invoice.customer_id, {
            "customer": invoice.customer,
            "total": ZERO,
            "oldest_days": 0,
            "invoices": 0,
            **{key: ZERO for key, *_ in AGEING_BUCKETS},
        })

        row[bucket] += invoice.owing
        row["total"] += invoice.owing
        row["invoices"] += 1
        row["oldest_days"] = max(row["oldest_days"], age)

        totals[bucket] += invoice.owing

    rows = sorted(
        customers.values(), key=lambda row: row["total"], reverse=True
    )

    grand_total = sum(totals.values(), ZERO)

    return {
        "rows": rows,
        "buckets": AGEING_BUCKETS,
        "totals": {key: money(value) for key, value in totals.items()},
        "total": money(grand_total),
        "overdue": money(totals["d61_90"] + totals["d90_plus"]),
        "as_at": as_at,
    }


def oldest_debts(limit=15, as_at=None):
    """The individual invoices that have been outstanding longest."""
    as_at = as_at or date.today()

    return [
        {
            "invoice": invoice,
            "owing": money(invoice.owing),
            "days": (as_at - invoice.date).days,
        }
        for invoice in _outstanding_invoices()[:limit]
    ]


# ------------------------------------------------- what a new cost lands on

def split_across_partners(amount):
    """One amount divided among the active partners by their shares.

    Used when something is bought or spent, so whoever entered it can see
    immediately whose money it was. The remainder from rounding is reported
    rather than dropped, for the same reason it is on the profit report.
    """
    partners = list(Partner.objects.filter(is_active=True))

    rows = [
        {"partner": partner, "amount": partner.share_of(amount)}
        for partner in partners
    ]

    allocated = sum((row["amount"] for row in rows), ZERO)

    shares = {row["amount"] for row in rows}

    return {
        "amount": money(amount),
        "rows": rows,
        "allocated": money(allocated),
        "rounding": money(amount - allocated),
        # Equal partners get one sentence rather than a list of four
        # identical numbers.
        "equal": len(shares) == 1 and len(rows) > 1,
        "each": rows[0]["amount"] if rows else ZERO,
        "count": len(rows),
    }


def describe_partner_cost(amount, kind="cost"):
    """A one-line explanation of whose money a purchase or expense was.

    Returns None when there are no partners on the books, so a business that
    has not recorded its owners is not told about shares that do not exist.
    """
    split = split_across_partners(amount)

    if not split["rows"]:
        return None

    if kind == "purchase":
        verb = f"ties up {split['amount']} of the partners' money in stock"
    else:
        verb = f"comes out of the partners' capital: {split['amount']}"

    if split["equal"]:
        detail = f"{split['each']} each, across {split['count']} partners"
    else:
        detail = ", ".join(
            f"{row['partner'].full_name} {row['amount']}"
            for row in split["rows"]
        )

    return f"This {verb} — {detail}."


# ------------------------------------------------------------------ payables

def supplier_payables():
    """What is owed to each supplier, oldest debt first.

    The mirror of the receivables ageing: a distributor that knows what it is
    owed but not what it owes has only half its cash position.
    """
    rows = []

    suppliers = Supplier.objects.prefetch_related(
        "purchases__items", "purchases__allocations", "payments"
    )

    for supplier in suppliers:
        billed = ZERO
        outstanding = ZERO
        oldest = None
        bills = 0

        for purchase in supplier.purchases.all():
            billed += purchase.total
            owing = purchase.balance

            if owing > ZERO:
                outstanding += owing
                bills += 1
                oldest = purchase.date if oldest is None else min(
                    oldest, purchase.date
                )

        paid = supplier.payments.aggregate(t=Sum("amount"))["t"] or ZERO

        if billed == ZERO and paid == ZERO:
            continue

        rows.append({
            "supplier": supplier,
            "billed": money(billed),
            "paid": money(paid),
            "outstanding": money(outstanding),
            "unpaid_bills": bills,
            "oldest": oldest,
            "oldest_days": (date.today() - oldest).days if oldest else 0,
        })

    rows.sort(key=lambda row: row["outstanding"], reverse=True)

    return {
        "rows": rows,
        "total_billed": money(sum((row["billed"] for row in rows), ZERO)),
        "total_paid": money(sum((row["paid"] for row in rows), ZERO)),
        "total_outstanding": money(
            sum((row["outstanding"] for row in rows), ZERO)
        ),
    }


def total_payables():
    """Everything still owed to suppliers, as one figure."""
    return supplier_payables()["total_outstanding"]


def unpaid_expenses():
    """Costs incurred and not yet paid out - a liability, not cash gone."""
    return _sum(
        Expense.objects.exclude(status=Expense.REJECTED).exclude(
            status=Expense.PAID
        ),
        "amount",
    )


# ------------------------------------------------------------ cash and bank

def cash_movements(account=None, start=None, end=None):
    """Every rupee in and out, oldest first.

    Gathered from the records that are themselves the movement - a customer
    payment, a supplier payment, a paid expense, capital in or out - rather
    than written to a second ledger that could drift out of step with them.

    Payroll is counted as paid in the month it was finalised. There is no
    record of a payslip being handed over, so that is the closest true thing
    the system knows.
    """
    rows = []

    def belongs(obj):
        return account is None or obj.account_id == account.pk

    for payment in _between(
        Payment.objects.select_related("customer", "account"),
        "paid_on", start, end,
    ):
        if belongs(payment):
            rows.append({
                "date": payment.paid_on,
                "kind": "Customer payment",
                "detail": payment.customer.name,
                "reference": payment.reference,
                "account": payment.account,
                "amount": payment.amount,
            })

    for payment in _between(
        SupplierPayment.objects.select_related("supplier", "account"),
        "date", start, end,
    ):
        if belongs(payment):
            rows.append({
                "date": payment.date,
                "kind": "Paid supplier",
                "detail": payment.supplier.name,
                "reference": payment.reference,
                "account": payment.account,
                "amount": -payment.amount,
            })

    for expense in _between(
        Expense.objects.filter(status=Expense.PAID).select_related(
            "category", "account"
        ),
        "date", start, end,
    ):
        if belongs(expense):
            rows.append({
                "date": expense.date,
                "kind": "Expense",
                "detail": expense.category.name,
                "reference": expense.reference,
                "account": expense.account,
                "amount": -expense.amount,
            })

    for entry in _between(
        CapitalTransaction.objects.select_related("partner", "account"),
        "date", start, end,
    ):
        if belongs(entry):
            rows.append({
                "date": entry.date,
                "kind": entry.get_kind_display(),
                "detail": entry.partner.full_name,
                "reference": entry.reference,
                "account": entry.account,
                "amount": entry.signed_amount,
            })

    # Payroll has no account of its own to sit in, so it is only included
    # when looking at the business as a whole.
    if account is None:
        for run in _between(
            PayrollRun.objects.filter(status=PayrollRun.FINALISED),
            "month", start, end,
        ):
            paid = run.total_net

            if paid:
                rows.append({
                    "date": run.month,
                    "kind": "Payroll",
                    "detail": run.month.strftime("%B %Y"),
                    "reference": "",
                    "account": None,
                    "amount": -paid,
                })

    rows.sort(key=lambda row: (row["date"], row["kind"]))

    return rows


def opening_balances(account=None):
    accounts = Account.objects.filter(is_active=True)

    if account is not None:
        accounts = accounts.filter(pk=account.pk)

    return _sum(accounts, "opening_balance")


def cash_book(account=None, start=None, end=None):
    """Movements with a running balance, and where it started from."""
    rows = cash_movements(account, start, end)

    # Anything before the window has already happened, so the balance the
    # window opens on has to include it - otherwise the closing figure is
    # wrong by exactly that much.
    brought_forward = opening_balances(account) + sum(
        (row["amount"] for row in cash_movements(account, None, None)
         if start is not None and row["date"] < start),
        ZERO,
    )

    running = brought_forward
    entries = []

    for row in rows:
        running += row["amount"]
        entries.append({**row, "balance": money(running)})

    money_in = sum((r["amount"] for r in rows if r["amount"] > ZERO), ZERO)
    money_out = sum((-r["amount"] for r in rows if r["amount"] < ZERO), ZERO)

    return {
        "entries": entries,
        "brought_forward": money(brought_forward),
        "money_in": money(money_in),
        "money_out": money(money_out),
        "closing": money(running),
        "account": account,
    }


def cash_on_hand():
    """What every account holds right now, in total."""
    return cash_book()["closing"]


def account_balances():
    """Each account and what is in it."""
    rows = []

    for account in Account.objects.filter(is_active=True):
        book = cash_book(account)

        rows.append({
            "account": account,
            "opening": money(account.opening_balance),
            "money_in": book["money_in"],
            "money_out": book["money_out"],
            "balance": book["closing"],
        })

    return rows


# ------------------------------------------------------------- balance sheet

def balance_sheet():
    """What the business owns, what it owes, and whose the rest is.

    The two sides are worked out independently and then set against each
    other, and any difference is printed rather than forced to nil. A balance
    sheet that has been made to balance by plugging the gap tells you nothing;
    one that shows a gap tells you exactly where to look.
    """
    cash = cash_on_hand()
    stock = stock_at_cost()
    owed_to_us = receivables()

    assets = cash + stock + owed_to_us

    owed_by_us = total_payables()
    unpaid = unpaid_expenses()

    liabilities = owed_by_us + unpaid

    opening = opening_balances()
    introduced = capital_introduced()
    withdrawn = capital_withdrawn()
    retained = profit_and_loss()["net_profit"]

    equity = opening + introduced - withdrawn + retained

    return {
        "cash": money(cash),
        "stock": money(stock),
        "receivables": money(owed_to_us),
        "assets": money(assets),

        "payables": money(owed_by_us),
        "unpaid_expenses": money(unpaid),
        "liabilities": money(liabilities),

        "net_assets": money(assets - liabilities),

        "opening_balances": money(opening),
        "capital_introduced": money(introduced),
        "capital_withdrawn": money(withdrawn),
        "retained_profit": money(retained),
        "equity": money(equity),

        # Zero when every movement has been recorded. Anything else is real
        # and worth chasing: stock received without a purchase behind it, or
        # a cost that never reached the books.
        "difference": money((assets - liabilities) - equity),
    }


def partner_equity():
    """The equity split by share, so each partner sees their piece of it."""
    sheet = balance_sheet()

    rows = []

    for partner in Partner.objects.filter(is_active=True):
        rows.append({
            "partner": partner,
            "share_percent": partner.share_percent,
            "capital": money(partner.net_contributed),
            "profit_share": partner.share_of(sheet["retained_profit"]),
            "total": money(
                partner.net_contributed
                + partner.share_of(sheet["retained_profit"])
            ),
        })

    return {"sheet": sheet, "rows": rows}
