import json
import tempfile
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import models
from .forms import EmployeeForm
from .models import (
    CallPoint,
    Customer,
    Employee,
    Invoice,
    InvoiceLog,
    Item,
    OVERDUE_DAYS,
    Payment,
    PlanVisit,
    Territory,
    UserRolls,
    WeeklyPlan,
    is_super_admin,
)
from .planning import generate_plan, monday_of
from .views import customers_with_balances, overdue_invoices


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

    def test_start_number_is_configurable(self):
        """Lets a fresh database continue past invoices already issued."""
        with mock.patch.object(models, "INVOICE_START_NUMBER", 9973):
            self.assertEqual(self.create_invoice().invoice_no, "HHC-9973")

    def test_start_number_ignored_once_invoices_exist(self):
        self.create_invoice()

        with mock.patch.object(models, "INVOICE_START_NUMBER", 5000):
            self.assertEqual(self.create_invoice().invoice_no, "HHC-9966")

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

    def test_logs_page_offers_a_way_out(self):
        """Super admins are blocked from '/', so the log page needs its own exit."""
        self.make_super_admin("boss4")
        self.client.login(username="boss4", password="pw")

        html = self.client.get(reverse("invoice_logs")).content.decode()

        self.assertIn(reverse("logout"), html)

    def test_logout_ends_the_session(self):
        self.make_super_admin("boss5")
        self.client.login(username="boss5", password="pw")

        response = self.client.get(reverse("logout"))

        self.assertRedirects(response, reverse("login"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_super_admin_is_redirected_away_from_the_form(self):
        self.make_super_admin("boss3")
        self.client.login(username="boss3", password="pw")

        response = self.client.get(reverse("index"))

        self.assertRedirects(response, reverse("invoice_logs"))


class FixtureRoundTripTests(TestCase):
    """Guards the Neon -> cPanel data move, which goes through dumpdata/loaddata."""

    def dump_and_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dump.json"

            call_command(
                "dumpdata", "auth.user", "invoices",
                natural_foreign=True, natural_primary=True,
                output=str(path), verbosity=0,
            )

            payload = json.loads(path.read_text())

            User.objects.all().delete()
            Customer.objects.all().delete()

            call_command("loaddata", str(path), verbosity=0)

        return payload

    def test_users_and_roles_survive_a_dump_and_reload(self):
        boss = User.objects.create_user("boss", password="pw")
        boss.userrolls.role = UserRolls.ROLE_SUPER_ADMIN
        boss.userrolls.save()
        User.objects.create_user("clerk", password="pw")

        self.dump_and_reload()

        self.assertEqual(User.objects.count(), 2)
        # The post_save signal must not add a second row alongside the fixture's
        self.assertEqual(UserRolls.objects.count(), 2)
        self.assertTrue(is_super_admin(User.objects.get(username="boss")))
        self.assertFalse(is_super_admin(User.objects.get(username="clerk")))

    def test_invoice_data_survives_and_numbering_continues(self):
        clerk = User.objects.create_user("clerk", password="pw")
        customer = Customer.objects.create(
            name="Shifa Pharmacy", address="Lahore", license_no="LIC-1"
        )
        invoice = Invoice.objects.create(customer=customer, license_no="LIC-1")
        Item.objects.create(
            invoice=invoice, name="Panadol", qty=10,
            batch="B1", expiry="12/26", price="100.00", discount="10.00",
        )
        InvoiceLog.objects.create(
            invoice=invoice, user=clerk, customer_name=customer.name, amount="900.00"
        )

        self.dump_and_reload()

        self.assertEqual(Invoice.objects.get().invoice_no, "HHC-9965")
        self.assertEqual(Item.objects.get().name, "Panadol")
        self.assertEqual(InvoiceLog.objects.get().amount, 900)
        self.assertEqual(Customer.objects.get().license_no, "LIC-1")
        # Fresh invoices must continue after the restored data, not restart
        self.assertEqual(Invoice.next_invoice_no(), "HHC-9966")


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


class CustomerEditTests(TestCase):

    def setUp(self):
        User.objects.create_user("clerk", password="pw")
        self.client.login(username="clerk", password="pw")

        self.customer = Customer.objects.create(name="Shifa Pharmacy", address="Lahore")

    def payload(self, **overrides):
        data = {
            "name": "Shifa Pharmacy",
            "address": "Lahore",
            "contact_person": "Dr. Ahmed Khan",
            "contact_number": "0300-1234567",
            "contact_email": "ahmed@example.com",
            "license_no": "LIC-1",
            "ntn": "1234567-8",
            "sales_tax": "ST-9",
        }
        data.update(overrides)

        return data

    def url(self):
        return reverse("customer_edit", args=[self.customer.pk])

    def test_contact_details_are_saved(self):
        response = self.client.post(self.url(), self.payload())

        self.assertRedirects(response, reverse("customer_list"))

        self.customer.refresh_from_db()
        self.assertEqual(self.customer.contact_person, "Dr. Ahmed Khan")
        self.assertEqual(self.customer.contact_number, "0300-1234567")
        self.assertEqual(self.customer.contact_email, "ahmed@example.com")

    def test_customer_can_be_renamed(self):
        self.client.post(self.url(), self.payload(name="Shifa Medical Store"))

        self.customer.refresh_from_db()
        self.assertEqual(self.customer.name, "Shifa Medical Store")

    def test_rename_onto_an_existing_name_is_rejected(self):
        Customer.objects.create(name="Al-Noor Pharmacy", address="Karachi")

        response = self.client.post(self.url(), self.payload(name="Al-Noor Pharmacy"))

        self.assertEqual(response.status_code, 200)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.name, "Shifa Pharmacy")

    def test_case_insensitive_duplicate_is_rejected(self):
        """Invoicing matches names exactly, so near-duplicates split a customer."""
        Customer.objects.create(name="Al-Noor Pharmacy", address="Karachi")

        response = self.client.post(self.url(), self.payload(name="AL-NOOR PHARMACY"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Customer.objects.filter(name="AL-NOOR PHARMACY").count(), 0)

    def test_blank_name_is_rejected(self):
        response = self.client.post(self.url(), self.payload(name="   "))

        self.assertEqual(response.status_code, 200)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.name, "Shifa Pharmacy")

    def test_list_shows_customers_with_invoice_counts(self):
        Invoice.objects.create(customer=self.customer, license_no="L")

        html = self.client.get(reverse("customer_list")).content.decode()

        self.assertIn("Shifa Pharmacy", html)
        self.assertIn(">1<", html)

    def test_list_search_filters_by_contact_person(self):
        self.customer.contact_person = "Dr. Ahmed Khan"
        self.customer.save()
        Customer.objects.create(name="Al-Noor Pharmacy", address="Karachi")

        html = self.client.get(
            reverse("customer_list"), {"q": "Ahmed"}
        ).content.decode()

        self.assertIn("Shifa Pharmacy", html)
        self.assertNotIn("Al-Noor Pharmacy", html)

    def test_login_is_required(self):
        self.client.logout()

        response = self.client.get(reverse("customer_list"))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])

    def test_invoicing_still_auto_creates_customers(self):
        """The edit screen supplements auto-creation, it does not replace it."""
        self.client.post(reverse("generate"), {
            "customer_name": "Brand New Pharmacy",
            "address": "Multan", "ntn": "", "sales_tax": "", "license_no": "LIC-77",
            "item_name[]": ["Panadol"], "qty[]": ["1"], "price[]": ["10"],
            "discount[]": ["0"], "batch[]": ["B"], "expiry[]": ["12/26"],
        })

        created = Customer.objects.get(name="Brand New Pharmacy")
        self.assertEqual(created.address, "Multan")
        self.assertEqual(created.license_no, "LIC-77")


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


class LedgerTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user("clerk", password="pw")
        self.client.login(username="clerk", password="pw")

        self.customer = Customer.objects.create(name="Shifa Pharmacy", address="Lahore")

    def invoice(self, total, days_ago=0):
        invoice = Invoice.objects.create(customer=self.customer, license_no="L")
        Invoice.objects.filter(pk=invoice.pk).update(
            total=Decimal(total),
            date=timezone.localdate() - timedelta(days=days_ago),
        )

        return Invoice.objects.get(pk=invoice.pk)

    def pay(self, amount, invoice=None):
        return Payment.objects.create(
            customer=self.customer,
            invoice=invoice,
            amount=Decimal(amount),
            recorded_by=self.user,
        )

    def test_balance_is_invoiced_minus_paid(self):
        self.invoice("1000.00")
        self.pay("400.00")

        self.assertEqual(self.customer.outstanding_balance, Decimal("600.00"))

    def test_partial_payment_leaves_invoice_balance(self):
        invoice = self.invoice("1000.00")
        self.pay("250.00", invoice=invoice)

        invoice.refresh_from_db()
        self.assertEqual(invoice.amount_paid, Decimal("250.00"))
        self.assertEqual(invoice.balance, Decimal("750.00"))
        self.assertFalse(invoice.is_paid)

    def test_multiple_partial_payments_settle_an_invoice(self):
        invoice = self.invoice("1000.00")
        self.pay("400.00", invoice=invoice)
        self.pay("600.00", invoice=invoice)

        self.assertTrue(Invoice.objects.get(pk=invoice.pk).is_paid)

    def test_account_payment_reduces_balance_without_an_invoice(self):
        self.invoice("500.00")
        self.pay("500.00")

        self.assertEqual(self.customer.outstanding_balance, Decimal("0.00"))

    def test_invoice_is_overdue_after_the_threshold(self):
        old = self.invoice("100.00", days_ago=OVERDUE_DAYS + 1)
        recent = self.invoice("100.00", days_ago=1)

        self.assertTrue(old.is_overdue)
        self.assertFalse(recent.is_overdue)

    def test_paid_invoice_is_never_overdue(self):
        old = self.invoice("100.00", days_ago=OVERDUE_DAYS + 5)
        self.pay("100.00", invoice=old)

        self.assertFalse(Invoice.objects.get(pk=old.pk).is_overdue)

    def test_overdue_list_excludes_settled_invoices(self):
        stale = self.invoice("100.00", days_ago=OVERDUE_DAYS + 2)
        settled = self.invoice("100.00", days_ago=OVERDUE_DAYS + 2)
        self.pay("100.00", invoice=settled)

        numbers = [i.invoice_no for i in overdue_invoices()]

        self.assertIn(stale.invoice_no, numbers)
        self.assertNotIn(settled.invoice_no, numbers)

    def test_statement_shows_a_running_balance(self):
        self.invoice("1000.00", days_ago=5)
        self.pay("300.00")

        response = self.client.get(
            reverse("customer_ledger", args=[self.customer.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["balance"], Decimal("700.00"))
        self.assertEqual(len(response.context["entries"]), 2)

    def test_recording_a_payment_updates_the_ledger(self):
        invoice = self.invoice("800.00")

        response = self.client.post(
            reverse("payment_create", args=[self.customer.pk]),
            {
                "invoice": invoice.pk,
                "amount": "300.00",
                "method": "cash",
                "reference": "",
                "paid_on": timezone.localdate().isoformat(),
                "note": "",
            },
        )

        self.assertRedirects(
            response, reverse("customer_ledger", args=[self.customer.pk])
        )
        self.assertEqual(self.customer.outstanding_balance, Decimal("500.00"))

    def test_overpaying_a_single_invoice_is_rejected(self):
        invoice = self.invoice("100.00")

        response = self.client.post(
            reverse("payment_create", args=[self.customer.pk]),
            {
                "invoice": invoice.pk,
                "amount": "500.00",
                "method": "cash",
                "reference": "",
                "paid_on": timezone.localdate().isoformat(),
                "note": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Payment.objects.count(), 0)

    def test_zero_or_negative_payments_are_rejected(self):
        response = self.client.post(
            reverse("payment_create", args=[self.customer.pk]),
            {
                "invoice": "",
                "amount": "0",
                "method": "cash",
                "reference": "",
                "paid_on": timezone.localdate().isoformat(),
                "note": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Payment.objects.count(), 0)

    def test_generated_invoice_stores_its_total(self):
        self.client.post(reverse("generate"), {
            "customer_name": "Shifa Pharmacy", "address": "Lahore",
            "ntn": "", "sales_tax": "", "license_no": "L",
            "item_name[]": ["Panadol"], "qty[]": ["10"], "price[]": ["100.00"],
            "discount[]": ["10"], "batch[]": ["B"], "expiry[]": ["12/26"],
        })

        invoice = Invoice.objects.latest("id")
        self.assertEqual(invoice.total, Decimal("900.00"))

    def test_previous_balance_is_exposed_to_the_invoice_form(self):
        self.invoice("1000.00", days_ago=OVERDUE_DAYS + 1)
        self.pay("250.00")

        data = self.client.get(
            reverse("customer_last_invoice", args=[self.customer.pk])
        ).json()

        self.assertEqual(data["previous_balance"], "750.00")
        self.assertEqual(data["overdue_count"], 1)


class ShellTests(TestCase):

    def setUp(self):
        User.objects.create_user("clerk", password="pw")
        self.client.login(username="clerk", password="pw")

    def test_pages_render_with_the_sidebar_and_user_menu(self):
        for name in ("dashboard", "customer_list", "ledger_list",
                     "payment_list", "profile", "search"):
            with self.subTest(page=name):
                html = self.client.get(reverse(name)).content.decode()

                self.assertIn('class="sidebar"', html)
                self.assertIn(reverse("logout"), html)
                self.assertIn(reverse("profile"), html)

    def test_search_finds_a_customer_by_licence_number(self):
        Customer.objects.create(
            name="Shifa Pharmacy", address="Mall Road", license_no="LIC-7788"
        )

        html = self.client.get(reverse("search"), {"q": "7788"}).content.decode()

        self.assertIn("Shifa Pharmacy", html)

    def test_search_finds_a_customer_by_address(self):
        Customer.objects.create(name="Al-Noor", address="Ferozepur Road, Lahore")

        html = self.client.get(reverse("search"), {"q": "Ferozepur"}).content.decode()

        self.assertIn("Al-Noor", html)

    def test_profile_updates_the_users_name(self):
        self.client.post(reverse("profile"), {
            "first_name": "Mustafa", "last_name": "Ali",
            "email": "m@example.com", "phone": "0300-1112223",
        })

        user = User.objects.get(username="clerk")
        self.assertEqual(user.get_full_name(), "Mustafa Ali")
        self.assertEqual(user.userrolls.phone, "0300-1112223")

    def test_initials_fall_back_to_the_username(self):
        profile = User.objects.get(username="clerk").userrolls

        self.assertEqual(profile.initials, "CL")


class LedgerAggregateTests(TestCase):
    """The list page aggregates in SQL; it must agree with the model properties."""

    def setUp(self):
        self.customer = Customer.objects.create(name="Test Pharmacy", address="x")

    def add_invoice(self, total):
        invoice = Invoice.objects.create(customer=self.customer, license_no="L")
        Invoice.objects.filter(pk=invoice.pk).update(total=Decimal(total))

    def test_repeated_amounts_are_not_collapsed(self):
        """Two invoices of the same value must count twice, not once."""
        self.add_invoice("100.00")
        self.add_invoice("100.00")
        Payment.objects.create(customer=self.customer, amount=Decimal("50.00"))
        Payment.objects.create(customer=self.customer, amount=Decimal("50.00"))

        row = customers_with_balances().get(pk=self.customer.pk)

        self.assertEqual(row.invoiced, Decimal("200.00"))
        self.assertEqual(row.paid, Decimal("100.00"))

    def test_invoices_and_payments_do_not_inflate_each_other(self):
        """Joining both in one query would multiply the totals out."""
        self.add_invoice("300.00")
        self.add_invoice("200.00")
        for _ in range(3):
            Payment.objects.create(customer=self.customer, amount=Decimal("10.00"))

        row = customers_with_balances().get(pk=self.customer.pk)

        self.assertEqual(row.invoiced, Decimal("500.00"))
        self.assertEqual(row.paid, Decimal("30.00"))

    def test_annotations_match_the_model_properties(self):
        self.add_invoice("125.50")
        self.add_invoice("125.50")
        Payment.objects.create(customer=self.customer, amount=Decimal("75.25"))

        row = customers_with_balances().get(pk=self.customer.pk)

        self.assertEqual(row.invoiced, self.customer.total_invoiced)
        self.assertEqual(row.paid, self.customer.total_paid)
        self.assertEqual(row.invoiced - row.paid, self.customer.outstanding_balance)

    def test_customer_with_no_activity_shows_zero(self):
        row = customers_with_balances().get(pk=self.customer.pk)

        self.assertEqual(row.invoiced, Decimal("0.00"))
        self.assertEqual(row.paid, Decimal("0.00"))


class TerritoryAndTeamTests(TestCase):

    def setUp(self):
        User.objects.create_user("clerk", password="pw")
        self.client.login(username="clerk", password="pw")

        self.territory = Territory.objects.create(name="Gulberg", city="Lahore")

    def test_employee_can_be_created_without_a_login(self):
        """Field staff often have no system account."""
        employee = Employee.objects.create(
            employee_code="MR-01", full_name="Ali Raza", territory=self.territory
        )

        self.assertIsNone(employee.user)
        self.assertTrue(employee.is_field_staff)

    def test_reporting_loop_is_rejected(self):
        boss = Employee.objects.create(employee_code="SM-01", full_name="Boss")
        mr = Employee.objects.create(
            employee_code="MR-01", full_name="Ali", reports_to=boss
        )
        boss.reports_to = mr
        boss.save()

        form = EmployeeForm(
            data={
                "employee_code": "MR-01", "full_name": "Ali",
                "designation": "mr", "phone": "", "email": "",
                "territory": "", "reports_to": boss.pk, "user": "",
                "joined_on": "", "is_active": True,
            },
            instance=mr,
        )

        self.assertFalse(form.is_valid())
        self.assertIn("reports_to", form.errors)

    def test_territory_report_totals_sales_by_location(self):
        customer = Customer.objects.create(
            name="Shifa", address="x", territory=self.territory
        )
        invoice = Invoice.objects.create(customer=customer, license_no="L")
        Invoice.objects.filter(pk=invoice.pk).update(total=Decimal("1000.00"))
        Payment.objects.create(customer=customer, amount=Decimal("400.00"))

        response = self.client.get(reverse("territory_report"))
        row = response.context["rows"][0]

        self.assertEqual(row["invoiced"], Decimal("1000.00"))
        self.assertEqual(row["received"], Decimal("400.00"))
        self.assertEqual(row["balance"], Decimal("600.00"))

    def test_report_warns_about_customers_with_no_territory(self):
        Customer.objects.create(name="Orphan", address="x")

        response = self.client.get(reverse("territory_report"))

        self.assertEqual(response.context["unassigned_customers"], 1)


class WeeklyPlanTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user("clerk", password="pw")
        self.client.login(username="clerk", password="pw")

        self.territory = Territory.objects.create(name="Gulberg", city="Lahore")
        self.mr = Employee.objects.create(
            employee_code="MR-01", full_name="Ali Raza",
            designation="mr", territory=self.territory,
        )

        self.week = monday_of(timezone.localdate())

    def make_call_points(self, count):
        return [
            CallPoint.objects.create(
                name=f"Dr. Test {i}", territory=self.territory, kind="doctor"
            )
            for i in range(count)
        ]

    def test_generator_spreads_visits_across_working_days(self):
        self.make_call_points(12)

        plan, created = generate_plan(self.mr, self.week, calls_per_day=2)

        self.assertEqual(created, 12)
        for day, _ in PlanVisit.DAY_CHOICES:
            self.assertEqual(plan.visits.filter(day=day).count(), 2)

    def test_generator_respects_the_daily_capacity(self):
        self.make_call_points(50)

        plan, created = generate_plan(self.mr, self.week, calls_per_day=3)

        # 3 per day across 6 working days
        self.assertEqual(created, 18)
        self.assertEqual(plan.visits.count(), 18)

    def test_never_visited_call_points_are_scheduled_first(self):
        old, fresh = self.make_call_points(2)

        previous = WeeklyPlan.objects.create(
            employee=self.mr, week_start=self.week - timedelta(days=7)
        )
        PlanVisit.objects.create(
            plan=previous, call_point=fresh, day=0, status="done"
        )

        plan, _ = generate_plan(self.mr, self.week, calls_per_day=1)

        first = plan.visits.order_by("day").first()
        self.assertEqual(first.call_point, old)

    def test_week_start_snaps_to_monday(self):
        self.make_call_points(1)
        wednesday = self.week + timedelta(days=2)

        plan, _ = generate_plan(self.mr, wednesday)

        self.assertEqual(plan.week_start, self.week)
        self.assertEqual(plan.week_start.weekday(), 0)

    def test_approved_plan_is_never_overwritten(self):
        self.make_call_points(4)
        plan, _ = generate_plan(self.mr, self.week)
        plan.status = WeeklyPlan.STATUS_APPROVED
        plan.save()

        original = plan.visit_count
        again, created = generate_plan(self.mr, self.week)

        self.assertEqual(created, 0)
        self.assertEqual(again.visit_count, original)

    def test_regenerating_a_draft_replaces_its_visits(self):
        self.make_call_points(6)

        generate_plan(self.mr, self.week, calls_per_day=1)
        plan, _ = generate_plan(self.mr, self.week, calls_per_day=1)

        self.assertEqual(plan.visits.count(), 6)

    def test_employee_without_a_territory_generates_nothing(self):
        loner = Employee.objects.create(employee_code="MR-99", full_name="No Area")

        plan, created = generate_plan(loner, self.week)

        self.assertEqual(created, 0)

    def test_inactive_call_points_are_skipped(self):
        points = self.make_call_points(3)
        points[0].is_active = False
        points[0].save()

        plan, created = generate_plan(self.mr, self.week)

        self.assertEqual(created, 2)

    def test_coverage_tracks_completed_visits(self):
        self.make_call_points(4)
        plan, _ = generate_plan(self.mr, self.week)

        visit = plan.visits.first()
        visit.status = "done"
        visit.save()

        self.assertEqual(plan.completed_count, 1)
        self.assertEqual(plan.coverage_percent, 25)

    def test_plan_can_be_submitted_and_approved(self):
        self.make_call_points(2)
        plan, _ = generate_plan(self.mr, self.week)

        self.client.post(reverse("plan_status", args=[plan.pk, "submit"]))
        plan.refresh_from_db()
        self.assertEqual(plan.status, WeeklyPlan.STATUS_SUBMITTED)
        self.assertFalse(plan.is_editable)

        self.client.post(reverse("plan_status", args=[plan.pk, "approve"]))
        plan.refresh_from_db()
        self.assertEqual(plan.status, WeeklyPlan.STATUS_APPROVED)
        self.assertEqual(plan.reviewed_by, self.user)

    def test_visit_can_be_marked_done_from_the_plan_page(self):
        self.make_call_points(1)
        plan, _ = generate_plan(self.mr, self.week)
        visit = plan.visits.first()

        self.client.post(reverse("visit_status", args=[visit.pk, "done"]))

        visit.refresh_from_db()
        self.assertEqual(visit.status, "done")

    def test_generate_view_creates_a_plan(self):
        self.make_call_points(3)

        response = self.client.post(reverse("plan_generate"), {
            "employee": self.mr.pk,
            "week_start": self.week.isoformat(),
            "calls_per_day": 6,
        })

        plan = WeeklyPlan.objects.get(employee=self.mr, week_start=self.week)
        self.assertRedirects(response, reverse("plan_detail", args=[plan.pk]))
        self.assertEqual(plan.visit_count, 3)

    def test_visits_by_day_covers_the_whole_week(self):
        self.make_call_points(1)
        plan, _ = generate_plan(self.mr, self.week)

        days = plan.visits_by_day()

        self.assertEqual(len(days), 6)
        self.assertEqual(days[0]["date"], self.week)
        self.assertEqual(days[5]["date"], self.week + timedelta(days=5))

    def test_field_force_pages_render(self):
        self.make_call_points(2)
        plan, _ = generate_plan(self.mr, self.week)

        for url in (
            reverse("team_list"), reverse("territory_list"),
            reverse("call_point_list"), reverse("plan_list"),
            reverse("plan_detail", args=[plan.pk]),
            reverse("territory_report"),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)
