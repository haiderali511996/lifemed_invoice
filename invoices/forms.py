from decimal import Decimal

from django import forms

from .models import (
    Batch,
    CallPoint,
    CallReport,
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
            "other_allowance", "commission_percent",
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


class BatchForm(forms.ModelForm):
    """Correct a batch's identity: its number and its expiry.

    Quantity is deliberately absent - it moves through the stock ledger via
    the adjustment screen, never by being typed over here.
    """

    class Meta:
        model = Batch
        fields = ["batch_no", "expiry_date"]
        widgets = {"expiry_date": forms.DateInput(attrs={"type": "date"})}

    def clean_batch_no(self):
        batch_no = self.cleaned_data["batch_no"].strip()

        clash = Batch.objects.filter(
            product=self.instance.product, batch_no=batch_no
        ).exclude(pk=self.instance.pk)

        if clash.exists():
            raise forms.ValidationError(
                f"{self.instance.product.name} already has a batch "
                f"“{batch_no}”. Two batches of one product cannot share a "
                f"number."
            )

        return batch_no


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

    def __init__(self, *args, employee=None, **kwargs):
        """`employee` is the MR issuing these samples, for their own login."""
        super().__init__(*args, **kwargs)

        self.employee = employee

        self.fields["employee"].queryset = Employee.objects.filter(is_active=True)

        call_points = CallPoint.objects.filter(
            is_active=True
        ).select_related("territory")

        if employee is not None:
            del self.fields["employee"]

            if employee.territory_id is not None:
                call_points = call_points.filter(territory=employee.territory)

        self.fields["call_point"].queryset = call_points
        self.fields["call_point"].required = False
        self.fields["call_point"].empty_label = "— Not recorded —"
        self.fields["note"].required = False

    def save(self, commit=True):
        issue_record = super().save(commit=False)

        if self.employee is not None:
            issue_record.employee = self.employee

        if commit:
            issue_record.save()

        return issue_record


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


class CallReportForm(forms.ModelForm):
    """What happened on a visit.

    A call point can be created here: an MR who meets a doctor who was never
    on the list must be able to record it without leaving the form.
    """

    new_call_point = forms.CharField(
        max_length=200, required=False,
        label="...or add a new call point",
        widget=forms.TextInput(
            attrs={"placeholder": "e.g. Dr. Sana Malik Clinic"}
        ),
    )
    new_call_point_kind = forms.ChoiceField(
        choices=CallPoint.KIND_CHOICES, required=False, initial="doctor",
        label="New call point type",
    )
    new_call_point_territory = forms.ModelChoiceField(
        queryset=Territory.objects.filter(is_active=True), required=False,
        label="New call point territory",
    )

    class Meta:
        model = CallReport
        fields = [
            "employee", "call_point", "visit_date", "visit_time",
            "doctor_name", "speciality", "outcome", "products",
            "feedback", "next_visit_date",
        ]
        widgets = {
            "visit_date": forms.DateInput(attrs={"type": "date"}),
            "visit_time": forms.TimeInput(attrs={"type": "time"}),
            "next_visit_date": forms.DateInput(attrs={"type": "date"}),
            "feedback": forms.Textarea(attrs={"rows": 3}),
            "products": forms.SelectMultiple(attrs={"size": 6}),
        }

    def __init__(self, *args, employee=None, **kwargs):
        """`employee` is the MR filing this report, when the login is theirs.

        Given one, the form stops asking who is reporting - they are - and
        narrows the doctor list to the patch they actually work.
        """
        super().__init__(*args, **kwargs)

        self.employee = employee

        # Set by clean() when a new call point is to be created, acted on by
        # save() so nothing is written before the whole form is known good.
        self.pending_call_point = None

        self.fields["employee"].queryset = Employee.objects.filter(is_active=True)

        call_points = CallPoint.objects.filter(
            is_active=True
        ).select_related("territory")

        if employee is not None:
            # Nobody files a visit under someone else's name from their own
            # login, so the field goes rather than being shown and ignored.
            del self.fields["employee"]

            if employee.territory_id is not None:
                call_points = call_points.filter(territory=employee.territory)

                self.fields["new_call_point_territory"].queryset = (
                    Territory.objects.filter(pk=employee.territory_id)
                )
                self.fields["new_call_point_territory"].initial = (
                    employee.territory_id
                )

        self.fields["call_point"].queryset = call_points
        self.fields["call_point"].required = False
        self.fields["call_point"].empty_label = "-- choose a call point --"

        self.fields["products"].queryset = Product.objects.filter(is_active=True)
        self.fields["products"].required = False

        for optional in ("visit_time", "doctor_name", "speciality", "feedback",
                         "next_visit_date"):
            self.fields[optional].required = False

    def clean(self):
        cleaned = super().clean()

        call_point = cleaned.get("call_point")
        new_name = (cleaned.get("new_call_point") or "").strip()

        if not call_point and not new_name:
            raise forms.ValidationError(
                "Pick a call point, or type a name to create a new one."
            )

        if not call_point and new_name:
            territory = cleaned.get("new_call_point_territory")

            if territory is None:
                employee = self.employee or cleaned.get("employee")
                territory = employee.territory if employee else None

            if territory is None:
                self.add_error(
                    "new_call_point_territory",
                    "Choose a territory - the MR has none assigned to fall back on.",
                )

                return cleaned

            # Only resolved here, not created. A form that fails validation
            # elsewhere must not leave a stray call point behind, so the write
            # waits for save().
            self.pending_call_point = {
                "name": new_name,
                "territory": territory,
                "kind": cleaned.get("new_call_point_kind") or "doctor",
            }

        return cleaned

    def save(self, commit=True):
        report = super().save(commit=False)

        call_point = self.cleaned_data.get("call_point")

        if call_point is None and self.pending_call_point is not None:
            # get_or_create keeps a repeated name in one territory as one record.
            call_point = CallPoint.objects.get_or_create(
                name=self.pending_call_point["name"],
                territory=self.pending_call_point["territory"],
                defaults={
                    "kind": self.pending_call_point["kind"],
                    "is_active": True,
                },
            )[0]

        report.call_point = call_point

        if self.employee is not None:
            report.employee = self.employee

        if commit:
            report.save()
            self.save_m2m()

        return report
