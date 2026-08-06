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
    def outstanding_balance(self):
        """What the customer owes across every invoice, less everything paid."""
        return self.total_invoiced - self.total_paid

    def overdue_invoices(self):
        cutoff = timezone.now().date() - timedelta(days=OVERDUE_DAYS)

        return [
            invoice
            for invoice in self.invoice_set.filter(date__lte=cutoff)
            if invoice.balance > ZERO
        ]


class Invoice(models.Model):
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE)

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
    def balance(self):
        return self.total - self.amount_paid

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
    def next_invoice_no(cls):
        """Highest existing number + 1, compared numerically rather than as text."""
        numbers = []

        for value in cls.objects.filter(
            invoice_no__startswith=f"{INVOICE_PREFIX}-"
        ).values_list("invoice_no", flat=True):

            suffix = value.rsplit("-", 1)[-1]

            if suffix.isdigit():
                numbers.append(int(suffix))

        new_num = max(numbers) + 1 if numbers else INVOICE_START_NUMBER

        return f"{INVOICE_PREFIX}-{new_num:04d}"

    def save(self, *args, **kwargs):
        if self.invoice_no:
            return super().save(*args, **kwargs)

        # Two people submitting at the same time can pick the same number, so
        # retry against the unique constraint instead of failing the request.
        original_pk = self.pk

        for attempt in range(INVOICE_NUMBER_ATTEMPTS):

            self.invoice_no = self.next_invoice_no()

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