from django.db import models, transaction, IntegrityError
from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver

INVOICE_PREFIX = "HHC"
INVOICE_START_NUMBER = 9965
INVOICE_NUMBER_ATTEMPTS = 5

class Customer(models.Model):
    name = models.CharField(max_length=255)
    address = models.TextField()
    ntn = models.CharField(max_length=50, blank=True, null=True)
    sales_tax = models.CharField(max_length=50, blank=True, null=True)
    license_no = models.CharField(max_length=100, blank=True, null=True)

    def __str__(self):
        return self.name


class Invoice(models.Model):
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE)

    # ✅ STRING FORMAT
    invoice_no = models.CharField(max_length=20, unique=True)

    date = models.DateField(auto_now_add=True)
    license_no = models.CharField(max_length=100)

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

    def __str__(self):
        return f"{self.user.username} - {self.role}"


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