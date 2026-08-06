from django import forms

from .models import Customer


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
