from django.db import models
from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver 

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

    def save(self, *args, **kwargs):
        if not self.invoice_no:
            last = Invoice.objects.order_by('-id').first()

            if last and last.invoice_no:
                last_num = int(last.invoice_no.split("-")[-1])
                new_num = last_num + 1
            else:
                new_num = 9965   # ✅ START

            self.invoice_no = f"HHC-{str(new_num).zfill(4)}"

        super().save(*args, **kwargs)


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
    ROLE_CHOICES = (
        ('super_admin', 'Super Admin'),
        ('manager', 'Manager'),
    )
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='manager')

    def __str__(self):
        return f"{self.user.username} - {self.role}"

# Signal: Jab bhi naya user banay, uska profile khud ban jaye
@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        UserRolls.objects.create(user=instance)

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