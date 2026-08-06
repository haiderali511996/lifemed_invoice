from decimal import Decimal

from django import forms

from .models import Customer, Invoice, Payment, UserRolls


class CustomerForm(forms.ModelForm):
    """Edits a customer created automatically during invoicing."""

    class Meta:
        model = Customer
        fields = [
            "name",
            "address",
            "contact_person",
            "contact_number",
            "contact_email",
            "license_no",
            "ntn",
            "sales_tax",
        ]
        widgets = {
            "address": forms.Textarea(attrs={"rows": 3}),
            "contact_person": forms.TextInput(
                attrs={"placeholder": "e.g. Dr. Ahmed Khan"}
            ),
            "contact_number": forms.TextInput(attrs={"placeholder": "e.g. 0300-1234567"}),
        }

    def clean_name(self):
        name = self.cleaned_data["name"].strip()

        if not name:
            raise forms.ValidationError("Customer name cannot be blank.")

        # Invoicing matches customers by exact name, so near-duplicates that
        # differ only by case or spacing would silently create a second record.
        clash = Customer.objects.filter(name__iexact=name)

        if self.instance.pk:
            clash = clash.exclude(pk=self.instance.pk)

        if clash.exists():
            raise forms.ValidationError(
                "Another customer already uses this name."
            )

        return name


class PaymentForm(forms.ModelForm):
    """Records money received, optionally against a specific invoice."""

    class Meta:
        model = Payment
        fields = ["invoice", "amount", "method", "reference", "paid_on", "note"]
        widgets = {
            "paid_on": forms.DateInput(attrs={"type": "date"}),
            "note": forms.Textarea(attrs={"rows": 2}),
            "amount": forms.NumberInput(attrs={"step": "0.01", "min": "0.01"}),
            "reference": forms.TextInput(
                attrs={"placeholder": "Cheque no. / transaction ID"}
            ),
        }

    def __init__(self, *args, customer=None, **kwargs):
        super().__init__(*args, **kwargs)

        self.customer = customer

        # Only this customer's invoices, unpaid ones first, so the common case
        # of settling an outstanding invoice is the easiest to pick.
        invoices = Invoice.objects.filter(customer=customer) if customer else Invoice.objects.none()

        self.fields["invoice"].queryset = invoices
        self.fields["invoice"].required = False
        self.fields["invoice"].empty_label = "— Against account (no specific invoice) —"

        self.fields["reference"].required = False
        self.fields["note"].required = False

    def clean_amount(self):
        amount = self.cleaned_data["amount"]

        if amount is None or amount <= Decimal("0"):
            raise forms.ValidationError("Amount must be greater than zero.")

        return amount

    def clean(self):
        cleaned = super().clean()

        invoice = cleaned.get("invoice")
        amount = cleaned.get("amount")

        if invoice and amount:
            # Overpaying a single invoice is almost always a misallocation;
            # the surplus belongs against the account instead.
            outstanding = invoice.balance

            if self.instance.pk:
                outstanding += self.instance.amount

            if amount > outstanding:
                raise forms.ValidationError(
                    f"{invoice.invoice_no} only has {outstanding:.2f} outstanding. "
                    f"Record the extra against the account instead."
                )

        return cleaned


class ProfileForm(forms.ModelForm):
    """The signed-in user's own details."""

    first_name = forms.CharField(max_length=150, required=False)
    last_name = forms.CharField(max_length=150, required=False)
    email = forms.EmailField(required=False)

    class Meta:
        model = UserRolls
        fields = ["avatar", "phone"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        user = self.instance.user

        self.fields["first_name"].initial = user.first_name
        self.fields["last_name"].initial = user.last_name
        self.fields["email"].initial = user.email

    def save(self, commit=True):
        profile = super().save(commit=False)

        user = profile.user
        user.first_name = self.cleaned_data["first_name"]
        user.last_name = self.cleaned_data["last_name"]
        user.email = self.cleaned_data["email"]

        if commit:
            user.save()
            profile.save()

        return profile
