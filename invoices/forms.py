from decimal import Decimal

from django import forms

from .models import (
    CallPoint,
    Customer,
    Distributor,
    Employee,
    Expense,
    ExpenseCategory,
    Invoice,
    Manufacturer,
    PayrollRun,
    SampleIssue,
    Payment,
    Product,
    Purchase,
    Supplier,
    Territory,
    UserRolls,
)


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
            "territory",
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


class TerritoryForm(forms.ModelForm):
    class Meta:
        model = Territory
        fields = ["code", "name", "city", "region", "is_active"]


class EmployeeForm(forms.ModelForm):
    class Meta:
        model = Employee
        fields = [
            "employee_code", "full_name", "designation", "phone", "email",
            "territory", "reports_to", "user", "joined_on", "is_active",
            "basic_salary", "fuel_allowance", "mobile_allowance",
            "other_allowance",
        ]
        widgets = {
            "joined_on": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["territory"].required = False
        self.fields["reports_to"].required = False
        self.fields["user"].required = False
        self.fields["user"].empty_label = "— No login account —"

        # An employee cannot report to themselves, nor to their own reports.
        if self.instance.pk:
            self.fields["reports_to"].queryset = Employee.objects.exclude(
                pk=self.instance.pk
            )

    def clean_reports_to(self):
        manager = self.cleaned_data.get("reports_to")

        if manager and self.instance.pk:
            seen = set()
            current = manager

            while current is not None:
                if current.pk == self.instance.pk:
                    raise forms.ValidationError(
                        "That would create a reporting loop."
                    )

                if current.pk in seen:
                    break

                seen.add(current.pk)
                current = current.reports_to

        return manager


class CallPointForm(forms.ModelForm):
    class Meta:
        model = CallPoint
        fields = [
            "name", "kind", "speciality", "territory", "customer",
            "address", "phone", "estimated_volume", "is_active",
        ]
        widgets = {"address": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["customer"].required = False
        self.fields["customer"].empty_label = "— Not an invoicing customer —"
        self.fields["speciality"].required = False


class PlanGenerateForm(forms.Form):
    """Picks who and which week to generate for."""

    employee = forms.ModelChoiceField(
        queryset=Employee.objects.filter(is_active=True, designation="mr")
    )
    week_start = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text="Any date in the target week; it snaps to that Monday.",
    )
    calls_per_day = forms.IntegerField(min_value=1, max_value=20, initial=6)


class DistributorForm(forms.ModelForm):
    """Register a company and read the coordinates off its invoice template."""

    class Meta:
        model = Distributor
        fields = [
            "name", "code", "address", "phone", "license_no", "ntn",
            "sales_tax", "template", "invoice_start_number",
            "is_active", "is_default",
        ]
        widgets = {"address": forms.Textarea(attrs={"rows": 2})}
        help_texts = {
            "template": "PDF of the blank invoice form. Must contain real text, "
                        "not a scan.",
        }

    def clean_code(self):
        code = self.cleaned_data["code"].strip().upper()

        if not code.isalnum():
            raise forms.ValidationError("Use letters and digits only, e.g. HHC.")

        clash = Distributor.objects.filter(code__iexact=code)

        if self.instance.pk:
            clash = clash.exclude(pk=self.instance.pk)

        if clash.exists():
            raise forms.ValidationError("Another distributor already uses this code.")

        return code

    def clean_template(self):
        template = self.cleaned_data.get("template")

        if template and hasattr(template, "name"):
            if not template.name.lower().endswith(".pdf"):
                raise forms.ValidationError("The template must be a PDF file.")

        return template


class SupplierForm(forms.ModelForm):
    class Meta:
        model = Supplier
        fields = [
            "name", "contact_person", "phone", "email", "address", "ntn",
            "is_active",
        ]
        widgets = {"address": forms.Textarea(attrs={"rows": 2})}


class ManufacturerForm(forms.ModelForm):
    class Meta:
        model = Manufacturer
        fields = [
            "name", "code", "contact_person", "phone", "email", "website",
            "address", "country", "drug_licence", "ntn", "note", "is_active",
        ]
        widgets = {
            "address": forms.Textarea(attrs={"rows": 2}),
            "note": forms.Textarea(attrs={"rows": 2}),
        }

    def clean_name(self):
        name = self.cleaned_data["name"].strip()

        if not name:
            raise forms.ValidationError("Manufacturer name cannot be blank.")

        clash = Manufacturer.objects.filter(name__iexact=name)

        if self.instance.pk:
            clash = clash.exclude(pk=self.instance.pk)

        if clash.exists():
            raise forms.ValidationError(
                "Another manufacturer already uses this name."
            )

        return name


class ProductForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = [
            "code", "name", "generic_name", "manufacturer", "registration_no",
            "pack_size", "trade_price", "reorder_level", "is_active",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["manufacturer"].queryset = Manufacturer.objects.filter(
            is_active=True
        )
        self.fields["manufacturer"].required = False
        self.fields["manufacturer"].empty_label = "— Not recorded —"


class PurchaseForm(forms.ModelForm):
    class Meta:
        model = Purchase
        fields = ["supplier", "reference", "date", "note"]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "note": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["supplier"].queryset = Supplier.objects.filter(is_active=True)
        self.fields["reference"].required = False
        self.fields["note"].required = False


class StockAdjustmentForm(forms.Form):
    """Correct a batch to a counted quantity."""

    counted_quantity = forms.IntegerField(min_value=0)
    note = forms.CharField(
        max_length=255, required=False,
        widget=forms.TextInput(attrs={"placeholder": "Reason for the adjustment"}),
    )


class ExpenseCategoryForm(forms.ModelForm):
    class Meta:
        model = ExpenseCategory
        fields = ["name", "code", "per_employee", "note", "is_active"]
        widgets = {"note": forms.Textarea(attrs={"rows": 2})}


class ExpenseForm(forms.ModelForm):
    class Meta:
        model = Expense
        fields = [
            "category", "employee", "territory", "date", "amount",
            "description", "reference", "receipt",
        ]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "amount": forms.NumberInput(attrs={"step": "0.01", "min": "0.01"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["category"].queryset = ExpenseCategory.objects.filter(
            is_active=True
        )
        self.fields["employee"].queryset = Employee.objects.filter(is_active=True)
        self.fields["employee"].required = False
        self.fields["employee"].empty_label = "— Company cost, not a claim —"
        self.fields["territory"].required = False
        self.fields["description"].required = False
        self.fields["reference"].required = False

    def clean_amount(self):
        amount = self.cleaned_data["amount"]

        if amount is None or amount <= Decimal("0"):
            raise forms.ValidationError("Amount must be greater than zero.")

        return amount

    def clean(self):
        cleaned = super().clean()

        category = cleaned.get("category")
        employee = cleaned.get("employee")

        # A fuel allowance with nobody attached cannot be reported per person,
        # which is the whole point of tracking it.
        if category and category.per_employee and not employee:
            self.add_error(
                "employee",
                f"{category.name} is claimed by a team member - pick one.",
            )

        return cleaned


class SampleIssueForm(forms.ModelForm):
    class Meta:
        model = SampleIssue
        fields = ["employee", "call_point", "date", "note"]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "note": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["employee"].queryset = Employee.objects.filter(is_active=True)
        self.fields["call_point"].queryset = CallPoint.objects.filter(
            is_active=True
        ).select_related("territory")
        self.fields["call_point"].required = False
        self.fields["call_point"].empty_label = "— Not recorded —"
        self.fields["note"].required = False


class PayrollRunForm(forms.ModelForm):
    class Meta:
        model = PayrollRun
        fields = ["month", "note"]
        widgets = {
            "month": forms.DateInput(attrs={"type": "date"}),
            "note": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["note"].required = False
        self.fields["month"].help_text = (
            "Any date in the month - it is stored as the 1st."
        )

    def clean_month(self):
        month = self.cleaned_data["month"].replace(day=1)

        clash = PayrollRun.objects.filter(month=month)

        if self.instance.pk:
            clash = clash.exclude(pk=self.instance.pk)

        if clash.exists():
            raise forms.ValidationError(
                f"Payroll for {month:%B %Y} already exists."
            )

        return month
