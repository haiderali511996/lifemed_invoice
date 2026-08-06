from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import Customer, Invoice, InvoiceLog, Item, UserRolls, is_super_admin


class InvoiceNumberTests(TestCase):

    def setUp(self):
        self.customer = Customer.objects.create(name="Acme Pharma", address="Karachi")

    def create_invoice(self):
        return Invoice.objects.create(customer=self.customer, license_no="L-1")

    def test_first_invoice_uses_start_number(self):
        self.assertEqual(self.create_invoice().invoice_no, "HHC-9965")

    def test_numbers_increment(self):
        self.create_invoice()
        self.assertEqual(self.create_invoice().invoice_no, "HHC-9966")

    def test_rollover_past_four_digits_is_numeric_not_alphabetical(self):
        """"HHC-9999" sorts above "HHC-10000" as text - the next must still be 10000."""
        Invoice.objects.create(
            customer=self.customer, license_no="L-1", invoice_no="HHC-9999"
        )

        self.assertEqual(self.create_invoice().invoice_no, "HHC-10000")

    def test_explicit_invoice_no_is_preserved(self):
        invoice = Invoice.objects.create(
            customer=self.customer, license_no="L-1", invoice_no="HHC-5000"
        )

        self.assertEqual(invoice.invoice_no, "HHC-5000")

    def test_collision_with_existing_number_is_retried(self):
        """Simulates a concurrent insert grabbing the number first."""
        original = Invoice.next_invoice_no.__func__
        state = {"raced": False}

        def racing_next(cls):
            number = original(cls)

            if not state["raced"]:
                state["raced"] = True
                Invoice.objects.create(
                    customer=self.customer, license_no="L-1", invoice_no=number
                )

            return number

        Invoice.next_invoice_no = classmethod(racing_next)
        try:
            invoice = self.create_invoice()
        finally:
            Invoice.next_invoice_no = classmethod(original)

        self.assertEqual(invoice.invoice_no, "HHC-9966")
        self.assertEqual(Invoice.objects.count(), 2)


class RoleTests(TestCase):

    def make_super_admin(self, username):
        user = User.objects.create_user(username, password="pw")
        user.userrolls.role = UserRolls.ROLE_SUPER_ADMIN
        user.userrolls.save()

        return user

    def test_new_user_defaults_to_manager(self):
        user = User.objects.create_user("manager1", password="pw")

        self.assertEqual(user.userrolls.role, UserRolls.ROLE_MANAGER)
        self.assertFalse(is_super_admin(user))

    def test_super_admin_role_is_recognised(self):
        user = self.make_super_admin("boss")

        self.assertTrue(is_super_admin(User.objects.get(pk=user.pk)))

    def test_django_superuser_counts_as_super_admin(self):
        user = User.objects.create_superuser("root", "root@example.com", "pw")

        self.assertTrue(is_super_admin(user))

    def test_manager_is_denied_the_logs_page(self):
        User.objects.create_user("manager2", password="pw")
        self.client.login(username="manager2", password="pw")

        response = self.client.get(reverse("invoice_logs"))

        self.assertRedirects(response, reverse("index"))

    def test_super_admin_sees_the_logs_page(self):
        self.make_super_admin("boss2")
        self.client.login(username="boss2", password="pw")

        response = self.client.get(reverse("invoice_logs"))

        self.assertEqual(response.status_code, 200)

    def test_super_admin_is_redirected_away_from_the_form(self):
        self.make_super_admin("boss3")
        self.client.login(username="boss3", password="pw")

        response = self.client.get(reverse("index"))

        self.assertRedirects(response, reverse("invoice_logs"))


class GenerateInvoiceTests(TestCase):

    def setUp(self):
        User.objects.create_user("clerk", password="pw")
        self.client.login(username="clerk", password="pw")

    def post(self, **overrides):
        payload = {
            "customer_name": "Acme Pharma",
            "address": "Karachi",
            "ntn": "1234567-8",
            "sales_tax": "ST-99",
            "license_no": "LIC-42",
            "item_name[]": ["Panadol", "Brufen"],
            "qty[]": ["10", "5"],
            "price[]": ["100.00", "250.00"],
            "discount[]": ["10", "0"],
            "batch[]": ["B1", "B2"],
            "expiry[]": ["12/26", "01/27"],
        }
        payload.update(overrides)

        return self.client.post(reverse("generate"), payload)

    def test_generates_a_pdf(self):
        response = self.post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(Item.objects.count(), 2)

    def test_short_batch_and_expiry_lists_do_not_crash(self):
        """A POST with fewer batch/expiry values than items used to IndexError."""
        response = self.post(**{"batch[]": ["B1"], "expiry[]": []})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Item.objects.count(), 2)

    def test_customer_license_no_is_saved(self):
        self.post()

        self.assertEqual(Customer.objects.get().license_no, "LIC-42")

    def test_existing_customer_is_updated_not_duplicated(self):
        Customer.objects.create(name="Acme Pharma", address="Old", license_no="OLD")

        self.post()

        customer = Customer.objects.get()
        self.assertEqual(Customer.objects.count(), 1)
        self.assertEqual(customer.address, "Karachi")
        self.assertEqual(customer.license_no, "LIC-42")

    def test_log_records_the_discounted_total(self):
        self.post()

        # (100 - 10%) * 10 + 250 * 5 = 900 + 1250
        self.assertEqual(InvoiceLog.objects.get().amount, 2150)

    def test_blank_rows_are_skipped(self):
        self.post(**{
            "item_name[]": ["Panadol", ""],
            "qty[]": ["10", ""],
            "price[]": ["100.00", ""],
            "discount[]": ["10", ""],
            "batch[]": ["B1", ""],
            "expiry[]": ["12/26", ""],
        })

        self.assertEqual(Item.objects.count(), 1)

    def test_login_is_required(self):
        self.client.logout()

        response = self.client.post(reverse("generate"), {})

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])


class CustomerLastInvoiceTests(TestCase):

    def setUp(self):
        User.objects.create_user("clerk", password="pw")
        self.client.login(username="clerk", password="pw")

        self.customer = Customer.objects.create(
            name="Acme Pharma",
            address="Karachi",
            ntn="1234567-8",
            sales_tax="ST-99",
            license_no="LIC-42",
        )

    def url(self, customer=None):
        return reverse(
            "customer_last_invoice",
            args=[(customer or self.customer).pk],
        )

    def add_invoice(self, item_name):
        invoice = Invoice.objects.create(customer=self.customer, license_no="LIC-42")
        Item.objects.create(
            invoice=invoice,
            name=item_name,
            qty=7,
            batch="B9",
            expiry="05/27",
            price="120.50",
            discount="15.00",
        )

        return invoice

    def test_returns_customer_details(self):
        data = self.client.get(self.url()).json()

        self.assertEqual(data["license_no"], "LIC-42")
        self.assertEqual(data["ntn"], "1234567-8")
        self.assertEqual(data["sales_tax"], "ST-99")
        self.assertEqual(data["address"], "Karachi")

    def test_customer_without_invoices_returns_empty_items(self):
        data = self.client.get(self.url()).json()

        self.assertEqual(data["items"], [])
        self.assertIsNone(data["last_invoice_no"])

    def test_returns_items_of_the_most_recent_invoice(self):
        self.add_invoice("Old Medicine")
        latest = self.add_invoice("New Medicine")

        data = self.client.get(self.url()).json()

        self.assertEqual(data["last_invoice_no"], latest.invoice_no)
        self.assertEqual(
            data["items"],
            [{
                "name": "New Medicine",
                "qty": 7,
                "price": "120.50",
                "discount": "15.00",
                "batch": "B9",
                "expiry": "05/27",
            }],
        )

    def test_null_batch_and_expiry_become_empty_strings(self):
        invoice = Invoice.objects.create(customer=self.customer, license_no="L")
        Item.objects.create(
            invoice=invoice, name="X", qty=1, price="1.00", discount="0.00"
        )

        item = self.client.get(self.url()).json()["items"][0]

        self.assertEqual(item["batch"], "")
        self.assertEqual(item["expiry"], "")

    def test_unknown_customer_is_404(self):
        self.assertEqual(self.client.get(self.url(), follow=True).status_code, 200)
        self.assertEqual(
            self.client.get(
                reverse("customer_last_invoice", args=[999999])
            ).status_code,
            404,
        )

    def test_login_is_required(self):
        self.client.logout()

        response = self.client.get(self.url())

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])
