import os
from datetime import timedelta
from decimal import Decimal

from django.db import models, transaction, IntegrityError
from django.contrib.auth.models import User
from django.db.models import Sum
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

# An invoice still unpaid after this many days is flagged as overdue.
OVERDUE_DAYS = 30

# Batches expiring within this many days are flagged for action.
EXPIRY_WARNING_DAYS = 90

ZERO = Decimal("0.00")

INVOICE_PREFIX = "HHC"

# Where numbering begins when the Invoice table is empty. Override in .env when
# starting on a fresh database so new invoices continue past the last number
# already issued to customers instead of reusing it.
INVOICE_START_NUMBER = int(os.getenv("INVOICE_START_NUMBER", "9965"))

INVOICE_NUMBER_ATTEMPTS = 5

class Customer(models.Model):
    # Unique because invoicing looks customers up by name; duplicates would
    # make get_or_create ambiguous and raise MultipleObjectsReturned.
    name = models.CharField(max_length=255, unique=True)
    address = models.TextField(blank=True)
    ntn = models.CharField("CNIC / NTN", max_length=50, blank=True, null=True)
    sales_tax = models.CharField(
        "Sales tax registration", max_length=50, blank=True, null=True
    )
    license_no = models.CharField(
        "Pharmacy licence", max_length=100, blank=True, null=True
    )

    contact_person = models.CharField(max_length=255, blank=True)
    contact_number = models.CharField(max_length=50, blank=True)
    contact_email = models.EmailField(blank=True)

    territory = models.ForeignKey(
        "Territory", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="customers",
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def total_invoiced(self):
        return self.invoice_set.aggregate(t=Sum("total"))["t"] or ZERO

    @property
    def total_paid(self):
        return self.payments.aggregate(t=Sum("amount"))["t"] or ZERO

    @property
    def total_returned(self):
        return self.returns.aggregate(t=Sum("total"))["t"] or ZERO

    @property
    def outstanding_balance(self):
        """Invoiced, less what has been paid and what has been credited back."""
        return self.total_invoiced - self.total_paid - self.total_returned

    def overdue_invoices(self):
        cutoff = timezone.now().date() - timedelta(days=OVERDUE_DAYS)

        return [
            invoice
            for invoice in self.invoice_set.filter(date__lte=cutoff)
            if invoice.balance > ZERO
        ]


class Invoice(models.Model):
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE)

    # Which company's form this invoice is printed on. Its code drives the
    # invoice number prefix, so each distributor has an independent series.
    distributor = models.ForeignKey(
        "Distributor", on_delete=models.PROTECT, null=True, blank=True,
        related_name="invoices",
    )

    # ✅ STRING FORMAT
    invoice_no = models.CharField(max_length=20, unique=True)

    date = models.DateField(auto_now_add=True)
    license_no = models.CharField(max_length=100)

    # Net payable, stored so ledgers and balances never have to recompute it
    # from line items (and stay correct if pricing rules ever change).
    total = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)

    class Meta:
        ordering = ["-id"]

    def __str__(self):
        return self.invoice_no

    @property
    def amount_paid(self):
        return self.payments.aggregate(t=Sum("amount"))["t"] or ZERO

    @property
    def amount_returned(self):
        return self.returns.aggregate(t=Sum("total"))["t"] or ZERO

    @property
    def balance(self):
        """What is still collectable: credited goods are no longer owed."""
        return self.total - self.amount_paid - self.amount_returned

    def returned_qty(self, item):
        """How much of one invoice line has already come back."""
        return (
            SalesReturnItem.objects.filter(item=item)
            .aggregate(t=Sum("qty"))["t"] or 0
        )

    @property
    def is_paid(self):
        return self.balance <= ZERO

    @property
    def days_outstanding(self):
        return (timezone.now().date() - self.date).days

    @property
    def is_overdue(self):
        return not self.is_paid and self.days_outstanding >= OVERDUE_DAYS

    @classmethod
    def next_invoice_no(cls, distributor=None):
        """Highest existing number + 1 for this distributor's series.

        Compared numerically rather than as text, so HHC-9999 is followed by
        HHC-10000 and not HHC-10000 sorting below it.
        """
        prefix = (distributor.code if distributor else INVOICE_PREFIX)

        start = (
            distributor.invoice_start_number
            if distributor else INVOICE_START_NUMBER
        )

        numbers = []

        for value in cls.objects.filter(
            invoice_no__startswith=f"{prefix}-"
        ).values_list("invoice_no", flat=True):

            suffix = value.rsplit("-", 1)[-1]

            if suffix.isdigit():
                numbers.append(int(suffix))

        new_num = max(numbers) + 1 if numbers else start

        return f"{prefix}-{new_num:04d}"

    def save(self, *args, **kwargs):
        if self.invoice_no:
            return super().save(*args, **kwargs)

        # Two people submitting at the same time can pick the same number, so
        # retry against the unique constraint instead of failing the request.
        original_pk = self.pk

        for attempt in range(INVOICE_NUMBER_ATTEMPTS):

            self.invoice_no = self.next_invoice_no(self.distributor)

            try:
                with transaction.atomic():
                    return super().save(*args, **kwargs)

            except IntegrityError:
                if attempt == INVOICE_NUMBER_ATTEMPTS - 1:
                    raise

                self.pk = original_pk


class Item(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="items")
    name = models.CharField(max_length=255)
    qty = models.IntegerField()
    batch = models.CharField(max_length=100, blank=True, null=True)
    expiry = models.CharField(max_length=20, blank=True, null=True)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    discount = models.DecimalField(max_digits=5, decimal_places=2)

    # Set when the line was picked from stock. Free-text lines leave these
    # null and move no stock, so ad-hoc items still work.
    product = models.ForeignKey(
        "Product", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="sold_items",
    )
    stock_batch = models.ForeignKey(
        "Batch", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="sold_items",
    )


class Payment(models.Model):
    """Money received from a customer.

    A payment may be allocated to one invoice or left against the account, so
    lump sums and part payments both work without forcing a split up front.
    """

    METHOD_CHOICES = (
        ("cash", "Cash"),
        ("cheque", "Cheque"),
        ("bank", "Bank Transfer"),
        ("other", "Other"),
    )

    customer = models.ForeignKey(
        Customer, on_delete=models.CASCADE, related_name="payments"
    )
    invoice = models.ForeignKey(
        Invoice,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payments",
        help_text="Leave blank to credit the account rather than one invoice.",
    )

    amount = models.DecimalField(max_digits=12, decimal_places=2)
    method = models.CharField(max_length=20, choices=METHOD_CHOICES, default="cash")
    reference = models.CharField(
        max_length=100, blank=True, help_text="Cheque number, transaction ID, etc."
    )
    paid_on = models.DateField(default=timezone.localdate)
    note = models.TextField(blank=True)

    recorded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-paid_on", "-id"]

    def __str__(self):
        return f"{self.customer.name} - {self.amount}"


# 1. User Role Model
class UserRolls(models.Model):
    ROLE_SUPER_ADMIN = 'super_admin'
    ROLE_MANAGER = 'manager'

    ROLE_CHOICES = (
        (ROLE_SUPER_ADMIN, 'Super Admin'),
        (ROLE_MANAGER, 'Manager'),
    )
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default=ROLE_MANAGER)

    avatar = models.ImageField(upload_to="avatars/", blank=True, null=True)
    phone = models.CharField(max_length=50, blank=True)

    def __str__(self):
        return f"{self.user.username} - {self.role}"

    @property
    def display_name(self):
        return self.user.get_full_name() or self.user.username

    @property
    def initials(self):
        """Fallback avatar when no picture has been uploaded."""
        name = self.user.get_full_name().strip()

        if name:
            parts = name.split()
            return (parts[0][0] + (parts[-1][0] if len(parts) > 1 else "")).upper()

        return self.user.username[:2].upper()


def is_super_admin(user):
    """Role lives in UserRolls; Django superusers always qualify."""
    if not user.is_authenticated:
        return False

    if user.is_superuser:
        return True

    role = getattr(user, 'userrolls', None)

    return role is not None and role.role == UserRolls.ROLE_SUPER_ADMIN


# Signal: Jab bhi naya user banay, uska profile khud ban jaye
@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, raw=False, **kwargs):
    # `raw` means loaddata is restoring a fixture, which carries its own
    # UserRolls rows. Creating one here would collide with them.
    if created and not raw:
        UserRolls.objects.get_or_create(user=instance)

# 2. Invoice Log Model
class InvoiceLog(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE)
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    customer_name = models.CharField(max_length=255) # Extra safety ke liye
    amount = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    action = models.CharField(max_length=100, default="Generated")
    timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.invoice.invoice_no} - {self.amount}"

# ------------------------------------------------------------------ GEOGRAPHY

class Territory(models.Model):
    """A field area (a "brick"). Everything location-wise hangs off this."""

    name = models.CharField(max_length=120, unique=True)
    code = models.CharField(
        max_length=20, blank=True,
        help_text="Short reference used on tour plans, e.g. N-01.",
    )
    city = models.CharField(max_length=80)
    region = models.CharField(
        max_length=80, blank=True, help_text="Zone or province grouping."
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["city", "code", "name"]
        verbose_name_plural = "territories"

    def __str__(self):
        return f"{self.code} {self.name}".strip() if self.code else self.name

    @property
    def sales_total(self):
        return (
            Invoice.objects.filter(customer__territory=self).aggregate(
                t=Sum("total")
            )["t"]
            or ZERO
        )


# ------------------------------------------------------------------ TEAM

class Employee(models.Model):
    """A staff member. The login is optional: field staff often have none."""

    DESIGNATION_CHOICES = (
        ("mr", "Medical Representative"),
        ("area_manager", "Area Manager"),
        ("sales_manager", "Sales Manager"),
        ("admin", "Admin / Office"),
    )

    user = models.OneToOneField(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="employee",
        help_text="Link to a login account, if this person uses the system.",
    )

    employee_code = models.CharField(max_length=30, unique=True)
    full_name = models.CharField(max_length=150)
    designation = models.CharField(
        max_length=30, choices=DESIGNATION_CHOICES, default="mr"
    )

    phone = models.CharField(max_length=50, blank=True)
    email = models.EmailField(blank=True)

    territory = models.ForeignKey(
        Territory, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="employees",
    )
    reports_to = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="reports",
    )

    joined_on = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    basic_salary = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    fuel_allowance = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    mobile_allowance = models.DecimalField(
        max_digits=12, decimal_places=2, default=ZERO
    )
    other_allowance = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)

    class Meta:
        ordering = ["full_name"]

    def __str__(self):
        return f"{self.full_name} ({self.employee_code})"

    @property
    def is_field_staff(self):
        return self.designation == "mr"


# ------------------------------------------------------------------ CALL POINTS

class CallPoint(models.Model):
    """Somewhere an MR visits: a doctor, a chemist or a hospital."""

    KIND_CHOICES = (
        ("doctor", "Doctor"),
        ("chemist", "Chemist / Pharmacy"),
        ("hospital", "Hospital"),
    )

    name = models.CharField(max_length=200)
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, default="doctor")
    speciality = models.CharField(max_length=120, blank=True)

    territory = models.ForeignKey(
        Territory, on_delete=models.CASCADE, related_name="call_points"
    )
    customer = models.ForeignKey(
        Customer, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="call_points",
        help_text="Link to the invoicing customer, when this is a buying pharmacy.",
    )

    address = models.TextField(blank=True)
    phone = models.CharField(max_length=50, blank=True)
    estimated_volume = models.CharField(
        max_length=50, blank=True,
        help_text="Rough size, e.g. '250+' doctors or '150+ Chemists'.",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        unique_together = [("name", "territory")]

    def __str__(self):
        return self.name

    def last_visit_date(self):
        visit = (
            PlanVisit.objects.filter(call_point=self, status="done")
            .order_by("-plan__week_start", "-day")
            .first()
        )

        return visit.visit_date if visit else None


# ------------------------------------------------------------------ WEEKLY PLAN

class WeeklyPlan(models.Model):
    """One MR's tour plan for one week, from draft through approval."""

    STATUS_DRAFT = "draft"
    STATUS_SUBMITTED = "submitted"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"

    STATUS_CHOICES = (
        (STATUS_DRAFT, "Draft"),
        (STATUS_SUBMITTED, "Submitted"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REJECTED, "Rejected"),
    )

    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name="weekly_plans"
    )
    week_start = models.DateField(help_text="Monday of the plan week.")

    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_DRAFT
    )
    notes = models.TextField(blank=True)

    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    reviewed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="reviewed_plans",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True)

    class Meta:
        ordering = ["-week_start", "employee__full_name"]
        unique_together = [("employee", "week_start")]

    def __str__(self):
        return f"{self.employee.full_name} - week of {self.week_start}"

    @property
    def week_end(self):
        return self.week_start + timedelta(days=6)

    @property
    def is_editable(self):
        return self.status in (self.STATUS_DRAFT, self.STATUS_REJECTED)

    @property
    def visit_count(self):
        return self.visits.count()

    @property
    def completed_count(self):
        return self.visits.filter(status="done").count()

    @property
    def coverage_percent(self):
        total = self.visit_count

        if not total:
            return 0

        return round(self.completed_count * 100 / total)

    def visits_by_day(self):
        """Visits grouped into the six working days the plan covers."""
        grouped = []

        for day, label in PlanVisit.DAY_CHOICES:
            grouped.append({
                "day": day,
                "label": label,
                "date": self.week_start + timedelta(days=day),
                "visits": list(self.visits.filter(day=day).select_related("call_point")),
            })

        return grouped


class PlanVisit(models.Model):
    """A single planned call, and how it actually went."""

    DAY_CHOICES = (
        (0, "Monday"),
        (1, "Tuesday"),
        (2, "Wednesday"),
        (3, "Thursday"),
        (4, "Friday"),
        (5, "Saturday"),
    )

    STATUS_CHOICES = (
        ("planned", "Planned"),
        ("done", "Visited"),
        ("missed", "Missed"),
    )

    plan = models.ForeignKey(
        WeeklyPlan, on_delete=models.CASCADE, related_name="visits"
    )
    call_point = models.ForeignKey(
        CallPoint, on_delete=models.CASCADE, related_name="visits"
    )

    day = models.IntegerField(choices=DAY_CHOICES)
    objective = models.CharField(max_length=255, blank=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="planned")
    remarks = models.TextField(blank=True)

    class Meta:
        ordering = ["day", "id"]
        unique_together = [("plan", "call_point", "day")]

    def __str__(self):
        return f"{self.call_point.name} - {self.get_day_display()}"

    @property
    def visit_date(self):
        return self.plan.week_start + timedelta(days=self.day)


# ------------------------------------------------------------------ DISTRIBUTORS

class Distributor(models.Model):
    """A company whose invoices this system prints.

    Each one has its own pre-printed PDF form. `layout` holds the coordinate
    map detected from that form (see invoices.layout), so adding a distributor
    is an upload rather than a code change.
    """

    name = models.CharField(max_length=200, unique=True)
    code = models.CharField(
        max_length=10,
        unique=True,
        help_text="Invoice number prefix, e.g. HHC for HHC-9965.",
    )

    address = models.TextField(blank=True)
    phone = models.CharField(max_length=50, blank=True)
    license_no = models.CharField("Drug licence", max_length=100, blank=True)
    ntn = models.CharField(max_length=50, blank=True)
    sales_tax = models.CharField(max_length=50, blank=True)

    template = models.FileField(
        upload_to="invoice_templates/",
        blank=True,
        help_text="The blank invoice PDF. Coordinates are read from it.",
    )
    layout = models.JSONField(
        blank=True,
        null=True,
        help_text="Detected coordinate map. Regenerated when the template changes.",
    )

    invoice_start_number = models.IntegerField(default=1)

    is_active = models.BooleanField(default=True)
    is_default = models.BooleanField(
        default=False, help_text="Pre-selected on the invoice form."
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self.code = self.code.upper().strip()

        super().save(*args, **kwargs)

        # Exactly one default, enforced here rather than trusted to the form.
        if self.is_default:
            Distributor.objects.exclude(pk=self.pk).update(is_default=False)

    @property
    def template_path(self):
        return self.template.path if self.template else None

    @property
    def has_layout(self):
        return bool(self.layout and self.layout.get("table"))

    @classmethod
    def default(cls):
        return (
            cls.objects.filter(is_active=True, is_default=True).first()
            or cls.objects.filter(is_active=True).first()
        )


# ------------------------------------------------------------------ PURCHASING

class Supplier(models.Model):
    name = models.CharField(max_length=200, unique=True)
    contact_person = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=50, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    ntn = models.CharField(max_length=50, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Manufacturer(models.Model):
    """Who makes a product. Distinct from Supplier, who sells it to us.

    The same manufacturer is often reached through several suppliers, and
    recalls and quality queries go to the manufacturer, so they are tracked
    separately.
    """

    name = models.CharField(max_length=200, unique=True)
    code = models.CharField(max_length=20, blank=True)

    contact_person = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=50, blank=True)
    email = models.EmailField(blank=True)
    website = models.URLField(blank=True)

    address = models.TextField(blank=True)
    country = models.CharField(max_length=80, blank=True, default="Pakistan")

    drug_licence = models.CharField(
        "Drug manufacturing licence", max_length=100, blank=True
    )
    ntn = models.CharField(max_length=50, blank=True)

    is_active = models.BooleanField(default=True)
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def product_count(self):
        return self.products.count()

    @property
    def stock_on_hand(self):
        return sum(product.stock_on_hand for product in self.products.all())


class Product(models.Model):
    """A sellable item. Stock is tracked per batch, not on the product."""

    code = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=255)

    manufacturer = models.ForeignKey(
        Manufacturer, on_delete=models.PROTECT, null=True, blank=True,
        related_name="products",
    )
    generic_name = models.CharField(
        max_length=255, blank=True,
        help_text="Active ingredient, e.g. Paracetamol 500mg.",
    )
    registration_no = models.CharField(
        "DRAP registration", max_length=100, blank=True
    )

    pack_size = models.CharField(max_length=50, blank=True)
    trade_price = models.DecimalField(max_digits=10, decimal_places=2, default=ZERO)
    is_active = models.BooleanField(default=True)

    reorder_level = models.IntegerField(
        default=0, help_text="Flag the product when total stock falls below this."
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.code})"

    @property
    def stock_on_hand(self):
        return self.batches.aggregate(t=Sum("quantity"))["t"] or 0

    @property
    def sellable_stock(self):
        """Excludes expired batches - they must not be sold."""
        return (
            self.batches.filter(expiry_date__gte=timezone.localdate())
            .aggregate(t=Sum("quantity"))["t"] or 0
        )

    @property
    def needs_reorder(self):
        return self.reorder_level > 0 and self.sellable_stock <= self.reorder_level


class Batch(models.Model):
    """A received lot, with its own expiry and cost."""

    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name="batches"
    )
    batch_no = models.CharField(max_length=100)
    expiry_date = models.DateField()

    cost_price = models.DecimalField(max_digits=10, decimal_places=2, default=ZERO)
    received_quantity = models.IntegerField(default=0)
    quantity = models.IntegerField(default=0, help_text="Currently on hand.")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["expiry_date", "batch_no"]
        unique_together = [("product", "batch_no")]
        verbose_name_plural = "batches"

    def __str__(self):
        return f"{self.product.name} / {self.batch_no}"

    @property
    def is_expired(self):
        return self.expiry_date < timezone.localdate()

    @property
    def days_to_expiry(self):
        return (self.expiry_date - timezone.localdate()).days

    @property
    def expires_soon(self):
        return not self.is_expired and self.days_to_expiry <= EXPIRY_WARNING_DAYS


class Purchase(models.Model):
    """Goods received from a supplier."""

    supplier = models.ForeignKey(
        Supplier, on_delete=models.PROTECT, related_name="purchases"
    )
    reference = models.CharField(
        max_length=100, blank=True, help_text="The supplier's invoice number."
    )
    date = models.DateField(default=timezone.localdate)
    note = models.TextField(blank=True)

    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"{self.supplier.name} - {self.date}"

    @property
    def total(self):
        return sum(
            (line.line_total for line in self.items.all()), ZERO
        )


class PurchaseItem(models.Model):
    purchase = models.ForeignKey(
        Purchase, on_delete=models.CASCADE, related_name="items"
    )
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    batch = models.ForeignKey(
        Batch, on_delete=models.PROTECT, related_name="purchase_items"
    )

    quantity = models.IntegerField()
    cost_price = models.DecimalField(max_digits=10, decimal_places=2, default=ZERO)

    @property
    def line_total(self):
        return self.cost_price * self.quantity


class StockMovement(models.Model):
    """Append-only stock ledger. Batch quantities are a cache of this."""

    PURCHASE = "purchase"
    SALE = "sale"
    ADJUSTMENT = "adjustment"
    RETURN = "return"
    SAMPLE = "sample"

    KIND_CHOICES = (
        (PURCHASE, "Purchase"),
        (SALE, "Sale"),
        (ADJUSTMENT, "Adjustment"),
        (RETURN, "Return"),
        (SAMPLE, "Sample"),
    )

    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name="movements"
    )
    batch = models.ForeignKey(
        Batch, on_delete=models.CASCADE, related_name="movements"
    )

    quantity = models.IntegerField(help_text="Positive in, negative out.")
    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    reference = models.CharField(max_length=100, blank=True)
    note = models.TextField(blank=True)

    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.batch} {self.quantity:+d} ({self.kind})"


# ------------------------------------------------------------------ RETURNS

class SalesReturn(models.Model):
    """Goods a customer sent back: a credit note.

    Deliberately not a deletion. The invoice stays as issued, the return
    credits the customer's ledger, and any restocked lines move back into
    stock through the same ledger as every other movement.
    """

    RETURN_PREFIX = "CN"

    invoice = models.ForeignKey(
        Invoice, on_delete=models.PROTECT, related_name="returns"
    )
    customer = models.ForeignKey(
        Customer, on_delete=models.CASCADE, related_name="returns"
    )

    return_no = models.CharField(max_length=20, unique=True)
    date = models.DateField(default=timezone.localdate)

    reason = models.TextField(blank=True)
    restock = models.BooleanField(
        default=True,
        help_text="Uncheck for damaged or expired goods that cannot be resold.",
    )

    # Credited amount, stored so the ledger never recomputes it.
    total = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)

    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-id"]

    def __str__(self):
        return self.return_no

    @classmethod
    def next_return_no(cls):
        numbers = []

        for value in cls.objects.values_list("return_no", flat=True):
            suffix = str(value).rsplit("-", 1)[-1]

            if suffix.isdigit():
                numbers.append(int(suffix))

        return f"{cls.RETURN_PREFIX}-{(max(numbers) + 1 if numbers else 1):04d}"

    def save(self, *args, **kwargs):
        if not self.return_no:
            self.return_no = self.next_return_no()

        super().save(*args, **kwargs)


class SalesReturnItem(models.Model):
    sales_return = models.ForeignKey(
        SalesReturn, on_delete=models.CASCADE, related_name="items"
    )
    item = models.ForeignKey(
        Item, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="returned_lines",
        help_text="The invoice line this came off.",
    )

    name = models.CharField(max_length=255)
    qty = models.IntegerField()
    price = models.DecimalField(max_digits=10, decimal_places=2, default=ZERO)
    discount = models.DecimalField(max_digits=5, decimal_places=2, default=ZERO)

    batch = models.ForeignKey(
        Batch, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="returned_lines",
    )

    @property
    def line_total(self):
        net = self.price - (self.price * self.discount / Decimal("100"))

        return (net * self.qty).quantize(ZERO)


# ------------------------------------------------------------------ EXPENSES

class ExpenseCategory(models.Model):
    """A kind of spend: fuel allowance, doctor refreshment, DRAP fees..."""

    name = models.CharField(max_length=120, unique=True)
    code = models.CharField(max_length=20, blank=True)

    per_employee = models.BooleanField(
        default=True,
        help_text="Tick when this is normally claimed by a team member. "
                  "Company costs such as DRAP fees are not.",
    )
    is_active = models.BooleanField(default=True)
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "expense categories"

    def __str__(self):
        return self.name


class Expense(models.Model):
    """One claim or company cost."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    PAID = "paid"

    STATUS_CHOICES = (
        (PENDING, "Pending"),
        (APPROVED, "Approved"),
        (REJECTED, "Rejected"),
        (PAID, "Paid"),
    )

    category = models.ForeignKey(
        ExpenseCategory, on_delete=models.PROTECT, related_name="expenses"
    )
    employee = models.ForeignKey(
        Employee, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="expenses",
        help_text="Leave blank for a company cost that is not claimed by anyone.",
    )
    territory = models.ForeignKey(
        Territory, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="expenses",
    )

    date = models.DateField(default=timezone.localdate)
    amount = models.DecimalField(max_digits=12, decimal_places=2)

    description = models.CharField(max_length=255, blank=True)
    reference = models.CharField(
        max_length=100, blank=True, help_text="Bill or voucher number."
    )
    receipt = models.FileField(upload_to="expense_receipts/", blank=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=PENDING)

    submitted_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name="submitted_expenses"
    )
    reviewed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="reviewed_expenses",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.CharField(max_length=255, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"{self.category.name} - {self.amount}"

    @property
    def is_settled(self):
        return self.status == self.PAID

    @property
    def counts_towards_spend(self):
        """Rejected claims are not money out of the door."""
        return self.status != self.REJECTED


# ------------------------------------------------------------------ SAMPLING

class SampleIssue(models.Model):
    """Samples an MR handed to a doctor, taken out of sellable stock."""

    SAMPLE_PREFIX = "SMP"

    employee = models.ForeignKey(
        Employee, on_delete=models.PROTECT, related_name="sample_issues"
    )
    call_point = models.ForeignKey(
        CallPoint, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="sample_issues",
        help_text="The doctor or hospital the samples were left with.",
    )

    reference = models.CharField(max_length=20, unique=True)
    date = models.DateField(default=timezone.localdate)
    note = models.TextField(blank=True)

    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-id"]

    def __str__(self):
        return self.reference

    @classmethod
    def next_reference(cls):
        numbers = []

        for value in cls.objects.values_list("reference", flat=True):
            suffix = str(value).rsplit("-", 1)[-1]

            if suffix.isdigit():
                numbers.append(int(suffix))

        return f"{cls.SAMPLE_PREFIX}-{(max(numbers) + 1 if numbers else 1):04d}"

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = self.next_reference()

        super().save(*args, **kwargs)

    @property
    def total_units(self):
        return self.items.aggregate(t=Sum("qty"))["t"] or 0

    @property
    def total_value(self):
        """What the samples cost, valued at batch cost."""
        return sum(
            (line.batch.cost_price * line.qty for line in self.items.all()),
            ZERO,
        )


class SampleIssueItem(models.Model):
    sample_issue = models.ForeignKey(
        SampleIssue, on_delete=models.CASCADE, related_name="items"
    )
    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, related_name="sampled_items"
    )
    batch = models.ForeignKey(
        Batch, on_delete=models.PROTECT, related_name="sampled_items"
    )
    qty = models.IntegerField()


# ------------------------------------------------------------------ PAYROLL

class PayrollRun(models.Model):
    """One month's payroll."""

    DRAFT = "draft"
    FINALISED = "finalised"

    STATUS_CHOICES = ((DRAFT, "Draft"), (FINALISED, "Finalised"))

    month = models.DateField(help_text="Any date in the month; stored as the 1st.")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=DRAFT)
    note = models.TextField(blank=True)

    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-month"]
        unique_together = [("month",)]

    def __str__(self):
        return self.month.strftime("%B %Y")

    def save(self, *args, **kwargs):
        # Normalise to the first of the month so one run per month is unique.
        if self.month:
            self.month = self.month.replace(day=1)

        super().save(*args, **kwargs)

    @property
    def is_editable(self):
        return self.status == self.DRAFT

    @property
    def total_net(self):
        return self.payslips.aggregate(t=Sum("net_pay"))["t"] or ZERO

    @property
    def total_gross(self):
        return self.payslips.aggregate(t=Sum("gross_pay"))["t"] or ZERO


class Payslip(models.Model):
    """One employee's pay for one month.

    Amounts are copied from the employee at generation time so a later raise
    never rewrites a slip that has already been handed out.
    """

    run = models.ForeignKey(
        PayrollRun, on_delete=models.CASCADE, related_name="payslips"
    )
    employee = models.ForeignKey(
        Employee, on_delete=models.PROTECT, related_name="payslips"
    )

    basic_salary = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    fuel_allowance = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    mobile_allowance = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    other_allowance = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)

    expense_reimbursement = models.DecimalField(
        max_digits=12, decimal_places=2, default=ZERO,
        help_text="Approved expenses for the month, paid with salary.",
    )

    tax_deduction = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    advance_deduction = models.DecimalField(
        max_digits=12, decimal_places=2, default=ZERO
    )
    other_deduction = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)

    gross_pay = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    net_pay = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)

    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["employee__full_name"]
        unique_together = [("run", "employee")]

    def __str__(self):
        return f"{self.employee.full_name} - {self.run}"

    @property
    def total_allowances(self):
        return (
            self.fuel_allowance + self.mobile_allowance + self.other_allowance
        )

    @property
    def total_deductions(self):
        return self.tax_deduction + self.advance_deduction + self.other_deduction

    def recalculate(self):
        self.gross_pay = (
            self.basic_salary + self.total_allowances + self.expense_reimbursement
        )
        self.net_pay = self.gross_pay - self.total_deductions

        return self


# ------------------------------------------------------------------ CALL REPORTS

class CallReport(models.Model):
    """What actually happened on a visit.

    Separate from PlanVisit so unplanned calls are first-class: an MR who meets
    a doctor who was never on the schedule still records it, and the report
    links back to the planned slot when there was one.
    """

    MET = "met"
    NOT_AVAILABLE = "not_available"
    RESCHEDULED = "rescheduled"

    OUTCOME_CHOICES = (
        (MET, "Doctor met"),
        (NOT_AVAILABLE, "Not available"),
        (RESCHEDULED, "Rescheduled"),
    )

    employee = models.ForeignKey(
        Employee, on_delete=models.PROTECT, related_name="call_reports"
    )
    call_point = models.ForeignKey(
        CallPoint, on_delete=models.PROTECT, related_name="call_reports"
    )
    plan_visit = models.OneToOneField(
        PlanVisit, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="report",
        help_text="The scheduled slot this fulfils, when it was planned.",
    )

    visit_date = models.DateField(default=timezone.localdate)
    visit_time = models.TimeField(null=True, blank=True)

    # The person actually seen. A hospital call point covers many doctors, so
    # the name belongs on the visit rather than on the call point.
    doctor_name = models.CharField(max_length=200, blank=True)
    speciality = models.CharField(max_length=120, blank=True)

    outcome = models.CharField(max_length=20, choices=OUTCOME_CHOICES, default=MET)

    products = models.ManyToManyField(
        Product, blank=True, related_name="call_reports",
        help_text="What was detailed on this call.",
    )

    sample_issue = models.ForeignKey(
        SampleIssue, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="call_reports",
    )

    feedback = models.TextField(blank=True)
    next_visit_date = models.DateField(null=True, blank=True)

    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-visit_date", "-visit_time", "-id"]

    def __str__(self):
        who = self.doctor_name or self.call_point.name

        return f"{who} - {self.visit_date}"

    @property
    def was_planned(self):
        return self.plan_visit_id is not None

    @property
    def samples_given(self):
        return self.sample_issue.total_units if self.sample_issue else 0
