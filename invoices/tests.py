import csv
import gzip
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import tempfile
from datetime import date, datetime, timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.db.models import ProtectedError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import models
from .forms import (
    DistributorForm, EmployeeForm, ExpenseForm, ManufacturerForm,
)
from .layout import LayoutError, detect_layout
from .models import (
    Batch,
    CallPoint,
    CallReport,
    Distributor,
    Customer,
    Employee,
    Expense,
    ExpenseCategory,
    Invoice,
    InvoiceLog,
    Item,
    Manufacturer,
    OVERDUE_DAYS,
    Payment,
    PayrollRun,
    Payslip,
    PlanVisit,
    Product,
    Purchase,
    PurchaseItem,
    SalesReturn,
    SampleIssue,
    SampleIssueItem,
    StockMovement,
    Supplier,
    Territory,
    UserRolls,
    WeeklyPlan,
    ZERO,
    is_super_admin,
)
from .pdf import rows_per_page
from .stock import StockError, adjust, allocate_fefo, issue, receive
from .planning import generate_plan, monday_of
from .views import (
    customers_with_balances, month_range, overdue_invoices,
    record_batch_correction,
)


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

        def racing_next(cls, distributor=None):
            number = original(cls, distributor)

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


class OverlongInputTests(TestCase):
    """MySQL runs STRICT_TRANS_TABLES: oversized values error instead of truncating.

    SQLite truncates silently, so these assert the clamping explicitly rather
    than relying on the database to be forgiving.
    """

    def setUp(self):
        User.objects.create_user("clerk", password="pw")
        self.client.login(username="clerk", password="pw")

    def post(self, **overrides):
        payload = {
            "customer_name": "Shifa Pharmacy", "address": "Lahore",
            "ntn": "1234567-8", "sales_tax": "ST-9", "license_no": "LIC-1",
            "item_name[]": ["Panadol"], "qty[]": ["10"], "price[]": ["100.00"],
            "discount[]": ["10"], "batch[]": ["B1"], "expiry[]": ["12/26"],
        }
        payload.update(overrides)

        return self.client.post(reverse("generate"), payload)

    def test_overlong_registration_fields_are_clipped(self):
        response = self.post(
            ntn="N" * 200, sales_tax="S" * 200, license_no="L" * 400
        )

        self.assertEqual(response.status_code, 200)
        customer = Customer.objects.get()
        self.assertEqual(len(customer.ntn), 50)
        self.assertEqual(len(customer.sales_tax), 50)
        self.assertEqual(len(customer.license_no), 100)

    def test_overlong_item_fields_are_clipped(self):
        response = self.post(**{
            "item_name[]": ["X" * 500], "batch[]": ["B" * 300],
            "expiry[]": ["12/2026 or thereabouts"],
        })

        self.assertEqual(response.status_code, 200)
        item = Item.objects.get()
        self.assertEqual(len(item.name), 255)
        self.assertEqual(len(item.batch), 100)
        self.assertEqual(len(item.expiry), 20)

    def test_overlong_customer_name_is_clipped(self):
        self.post(customer_name="P" * 500)

        self.assertEqual(len(Customer.objects.get().name), 255)

    def test_blank_customer_name_is_rejected_not_crashed(self):
        response = self.post(customer_name="   ")

        self.assertRedirects(response, reverse("index"))
        self.assertEqual(Customer.objects.count(), 0)
        self.assertEqual(Invoice.objects.count(), 0)

    def test_absurd_price_is_capped_within_the_column(self):
        response = self.post(**{"price[]": ["9" * 20], "qty[]": ["1"]})

        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(Item.objects.get().price, Decimal("99999999.99"))

    def test_discount_above_100_percent_is_capped(self):
        self.post(**{"discount[]": ["999999"]})

        self.assertEqual(Item.objects.get().discount, Decimal("100.00"))

    def test_junk_numbers_do_not_crash(self):
        response = self.post(**{
            "qty[]": ["abc"], "price[]": ["not-a-number"], "discount[]": ["--"],
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Item.objects.get().qty, 0)

    def test_invoice_total_is_rounded_to_two_places(self):
        """A repeating discount produced more decimals than the column holds."""
        self.post(**{"price[]": ["33.33"], "qty[]": ["3"], "discount[]": ["7"]})

        invoice = Invoice.objects.get()
        self.assertEqual(invoice.total.as_tuple().exponent, -2)
        self.assertEqual(invoice.total, InvoiceLog.objects.get().amount)

    def test_negative_quantity_does_not_become_negative_stock(self):
        self.post(**{"qty[]": ["-5"]})

        self.assertEqual(Item.objects.get().qty, 0)


class PdfResponseTests(TestCase):
    """Passenger (cPanel) streams responses through wsgi.file_wrapper.

    Its wrapper calls fileno() on whatever it is given. FileResponse hands over
    the raw object, and a BytesIO has no file descriptor, so the download died
    with "io.UnsupportedOperation: fileno" in production while passing every
    test here. These assert the response is a plain in-memory body.
    """

    def setUp(self):
        User.objects.create_user("clerk", password="pw")
        self.client.login(username="clerk", password="pw")

    def generate(self):
        return self.client.post(reverse("generate"), {
            "customer_name": "Shifa Pharmacy", "address": "Lahore",
            "ntn": "1", "sales_tax": "2", "license_no": "LIC-1",
            "item_name[]": ["Panadol"], "qty[]": ["10"], "price[]": ["100.00"],
            "discount[]": ["10"], "batch[]": ["B1"], "expiry[]": ["12/26"],
        })

    def test_response_is_not_streaming(self):
        response = self.generate()

        self.assertFalse(response.streaming)
        self.assertTrue(hasattr(response, "content"))

    def test_response_carries_no_file_object(self):
        """file_to_stream is what makes a server reach for file_wrapper."""
        response = self.generate()

        self.assertIsNone(getattr(response, "file_to_stream", None))

    def test_body_survives_a_file_wrapper_that_demands_fileno(self):
        """Reproduces Passenger: wrap the body the way its server would."""
        response = self.generate()

        class PassengerFileWrapper:
            def __init__(self, filelike, blksize=8192):
                # Passenger asks for the descriptor before streaming.
                filelike.fileno()

        body = response.content

        # A streaming response would hand file_to_stream to this and blow up.
        streamed = getattr(response, "file_to_stream", None)

        if streamed is not None:
            with self.assertRaises(Exception):
                PassengerFileWrapper(streamed)

        self.assertTrue(body.startswith(b"%PDF"))

    def test_download_is_named_after_the_invoice(self):
        response = self.generate()
        invoice = Invoice.objects.get()

        self.assertEqual(
            response["Content-Disposition"],
            f'attachment; filename="{invoice.invoice_no}.pdf"',
        )

    def test_pdf_contains_the_invoice_details(self):
        response = self.generate()

        import pymupdf
        text = pymupdf.open(stream=response.content, filetype="pdf")[0].get_text()

        self.assertIn("Shifa Pharmacy", text)
        self.assertIn("Panadol", text)
        self.assertIn("900.00", text)


class InvoicePdfLayoutTests(TestCase):
    """The template is a fixed form: rows must stay inside the item table.

    Only three rows fit between the column headers (ending y=206.15) and the
    rule that closes the table (y=240.7). Overflowing past it used to white out
    the form's own totals labels. The bounds come from the detected layout
    rather than constants, so this holds for any distributor's template.
    """

    def setUp(self):
        User.objects.create_user("clerk", password="pw")
        self.client.login(username="clerk", password="pw")

        self.layout = detect_layout("template.pdf")
        self.per_page = rows_per_page(self.layout)
        self.rule_y = self.layout["table"]["bottom"]
        self.header_bottom = self.layout["table"]["header_bottom"]

    def generate(self, item_count, customer="Shifa Pharmacy"):
        n = item_count

        response = self.client.post(reverse("generate"), {
            "customer_name": customer, "address": "Mall Road, Lahore",
            "ntn": "1234567-8", "sales_tax": "ST-9", "license_no": "LIC-2211",
            "item_name[]": [f"Medicine {i}" for i in range(n)],
            "qty[]": ["2"] * n, "price[]": ["100"] * n,
            "discount[]": ["10"] * n,
            "batch[]": [f"B{i}" for i in range(n)],
            "expiry[]": ["12/26"] * n,
        })

        import pymupdf
        return pymupdf.open(stream=response.content, filetype="pdf")

    def all_text(self, doc):
        return "".join(page.get_text() for page in doc)

    def test_short_invoice_is_a_single_page(self):
        self.assertEqual(self.generate(self.per_page).page_count, 1)

    def test_long_invoice_paginates(self):
        doc = self.generate(self.per_page + 1)

        self.assertEqual(doc.page_count, 2)

    def test_every_item_appears_exactly_once(self):
        import re

        doc = self.generate(10)
        text = self.all_text(doc)

        for i in range(10):
            matches = re.findall(rf"Medicine {i}\b", text)
            self.assertEqual(len(matches), 1, f"Medicine {i}")

    def test_form_labels_survive_a_long_invoice(self):
        """The overflow used to erase these; that is the whole bug."""
        text = self.all_text(self.generate(25))

        for label in ("Sr#", "Qty", "Batch", "Expiry", "T.Price", "Disc%",
                      "Company", "Gross", "Discount", "Net", "Payable"):
            self.assertIn(label, text, label)

    def test_no_row_crosses_the_table_rule(self):
        doc = self.generate(12)

        for page in doc:
            for word in page.get_text("words"):
                if word[4].startswith("Medicine"):
                    self.assertLess(word[3], self.rule_y, word[4])
                    self.assertGreater(word[1], self.header_bottom, word[4])

    def test_totals_appear_only_on_the_final_page(self):
        doc = self.generate(10)          # 10 x 2 x 90 = 1800.00
        pages = [i for i, p in enumerate(doc) if "1800.00" in p.get_text()]

        self.assertEqual(pages, [doc.page_count - 1])

    def test_earlier_pages_say_continued(self):
        doc = self.generate(10)

        self.assertIn("Continued on page 2", doc[0].get_text())
        self.assertNotIn("Continued", doc[-1].get_text())

    def test_pages_are_numbered_when_there_is_more_than_one(self):
        doc = self.generate(10)

        self.assertIn(f"Page 1 of {doc.page_count}", doc[0].get_text())

    def test_single_page_invoice_is_not_numbered(self):
        self.assertNotIn("Page 1 of", self.generate(2)[0].get_text())

    def test_item_numbering_continues_across_pages(self):
        doc = self.generate(7)

        # Sr# runs 1..7 with three per page, so page 2 starts at 4
        self.assertIn("4", doc[1].get_text())
        self.assertEqual(Item.objects.count(), 7)


class PreviousBalanceOnInvoiceTests(TestCase):

    def setUp(self):
        User.objects.create_user("clerk", password="pw")
        self.client.login(username="clerk", password="pw")

    def generate(self, items=1):
        n = items

        response = self.client.post(reverse("generate"), {
            "customer_name": "Shifa Pharmacy", "address": "Lahore",
            "ntn": "1", "sales_tax": "2", "license_no": "L",
            "item_name[]": [f"Medicine {i}" for i in range(n)],
            "qty[]": ["2"] * n, "price[]": ["100"] * n,
            "discount[]": ["10"] * n, "batch[]": ["B"] * n,
            "expiry[]": ["12/26"] * n,
        })

        import pymupdf
        return pymupdf.open(stream=response.content, filetype="pdf")[-1].get_text()

    def test_first_invoice_has_no_previous_balance_block(self):
        self.assertNotIn("PREVIOUS OUTSTANDING", self.generate())

    def test_second_invoice_shows_the_first_as_outstanding(self):
        self.generate()                      # HHC-9965, 180.00
        text = self.generate()               # HHC-9966

        self.assertIn("PREVIOUS OUTSTANDING", text)
        self.assertIn("HHC-9965", text)
        self.assertIn("180.00", text)

    def test_each_previous_balance_carries_its_invoice_number(self):
        self.generate()
        self.generate()
        text = self.generate()

        self.assertIn("HHC-9965", text)
        self.assertIn("HHC-9966", text)
        self.assertNotIn("HHC-9968", text)   # not yet issued

    def test_grand_total_is_previous_plus_this_invoice(self):
        self.generate()                      # 180.00 outstanding
        text = self.generate()               # this one is 180.00

        self.assertIn("360.00", text)        # grand total

    def test_part_payment_reduces_the_carried_balance(self):
        self.generate()

        invoice = Invoice.objects.get()
        Payment.objects.create(
            customer=invoice.customer, invoice=invoice, amount=Decimal("100.00")
        )

        text = self.generate()

        self.assertIn("80.00", text)         # 180 - 100 carried forward

    def test_settled_invoices_are_not_listed(self):
        self.generate()

        invoice = Invoice.objects.get()
        Payment.objects.create(
            customer=invoice.customer, invoice=invoice, amount=invoice.total
        )

        self.assertNotIn("PREVIOUS OUTSTANDING", self.generate())

    def test_account_payments_are_shown_as_a_credit(self):
        """A lump sum not tied to an invoice must reconcile the listing."""
        self.generate()

        invoice = Invoice.objects.get()
        Payment.objects.create(customer=invoice.customer, amount=Decimal("50.00"))

        text = self.generate()

        self.assertIn("payments on account", text)
        self.assertIn("130.00", text)        # 180 - 50 actually owed


class BackupCommandTests(TestCase):
    """Backups hold every customer record and password hash, so this checks
    both that a dump is usable and that it is not left world-readable."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.out = Path(self.tmp) / "backups"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_backup(self, **kwargs):
        call_command("backup_db", output_dir=str(self.out), quiet=True, **kwargs)

        return sorted(self.out.glob("backup-*"))

    def test_creates_a_compressed_dump(self):
        files = self.run_backup()

        self.assertEqual(len(files), 1)
        self.assertGreater(files[0].stat().st_size, 0)

    def test_dump_is_a_readable_database(self):
        Customer.objects.create(name="Shifa Pharmacy", address="Lahore")

        dump = self.run_backup()[0]

        with gzip.open(dump, "rt", encoding="utf-8") as src:
            script = src.read()

        restored = sqlite3.connect(":memory:")
        restored.executescript(script)
        names = [
            row[0] for row in restored.execute("SELECT name FROM invoices_customer")
        ]
        restored.close()

        self.assertIn("Shifa Pharmacy", names)

    def test_dump_is_not_readable_by_others(self):
        dump = self.run_backup()[0]

        self.assertEqual(stat.S_IMODE(dump.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o700)

    def test_old_dumps_are_pruned(self):
        self.run_backup()

        stale = self.out / "backup-20200101-000000.sql.gz"
        stale.write_bytes(b"old")
        old = (datetime.now() - timedelta(days=90)).timestamp()
        os.utime(stale, (old, old))

        self.run_backup(keep_days=14)

        self.assertFalse(stale.exists())

    def test_recent_dumps_are_kept(self):
        self.run_backup()
        self.run_backup()

        self.assertEqual(len(sorted(self.out.glob("backup-*"))), 2)

    def test_keep_days_zero_disables_pruning(self):
        stale = self.out
        stale.mkdir(parents=True, exist_ok=True)
        keeper = stale / "backup-20200101-000000.sql.gz"
        keeper.write_bytes(b"old")
        old = (datetime.now() - timedelta(days=900)).timestamp()
        os.utime(keeper, (old, old))

        self.run_backup(keep_days=0)

        self.assertTrue(keeper.exists())

    def test_mysql_without_mysqldump_fails_loudly(self):
        """A silent failure here means discovering there are no backups too late."""
        mysql = {
            "ENGINE": "django.db.backends.mysql", "NAME": "db",
            "USER": "u", "PASSWORD": "p", "HOST": "localhost", "PORT": "3306",
        }

        with mock.patch.dict(settings.DATABASES, {"default": mysql}):
            with mock.patch("shutil.which", return_value=None):
                with self.assertRaises(CommandError) as caught:
                    self.run_backup()

        self.assertIn("mysqldump", str(caught.exception))

    def test_mysql_password_is_not_passed_on_the_command_line(self):
        """`ps` is readable by other accounts on shared hosting."""
        mysql = {
            "ENGINE": "django.db.backends.mysql", "NAME": "db",
            "USER": "u", "PASSWORD": "s3cret", "HOST": "localhost", "PORT": "",
        }
        seen = {}

        def fake_run(command, **kwargs):
            seen["command"] = command
            defaults = [a for a in command if a.startswith("--defaults-extra-file=")]
            seen["cnf"] = Path(defaults[0].split("=", 1)[1]).read_text()
            kwargs["stdout"].write(b"-- dump")

            return subprocess.CompletedProcess(command, 0, stderr=b"")

        with mock.patch.dict(settings.DATABASES, {"default": mysql}):
            with mock.patch("shutil.which", return_value="/usr/bin/mysqldump"):
                with mock.patch("subprocess.run", side_effect=fake_run):
                    self.run_backup()

        self.assertNotIn("s3cret", " ".join(seen["command"]))
        self.assertIn("password=s3cret", seen["cnf"])

    def test_failed_mysqldump_does_not_leave_a_broken_file(self):
        mysql = {
            "ENGINE": "django.db.backends.mysql", "NAME": "db",
            "USER": "u", "PASSWORD": "p", "HOST": "localhost", "PORT": "",
        }

        def failing_run(command, **kwargs):
            return subprocess.CompletedProcess(command, 2, stderr=b"access denied")

        with mock.patch.dict(settings.DATABASES, {"default": mysql}):
            with mock.patch("shutil.which", return_value="/usr/bin/mysqldump"):
                with mock.patch("subprocess.run", side_effect=failing_run):
                    with self.assertRaises(CommandError):
                        self.run_backup()

        self.assertEqual(sorted(self.out.glob("backup-*")), [])


class LayoutDetectionTests(TestCase):
    """Coordinates are found by locating the labels the form already prints,
    so a new distributor is an upload rather than a code change."""

    def setUp(self):
        self.layout = detect_layout("template.pdf")

    def test_finds_the_header_fields(self):
        for field in ("customer_name", "address", "invoice_no", "date",
                      "license_no", "ntn", "sales_tax"):
            self.assertIn(field, self.layout["fields"], field)

    def test_matches_the_hand_measured_coordinates(self):
        """Guards the detector against drifting away from the known-good map."""
        expected = {
            "customer_name": 125.84, "address": 125.84,
            "invoice_no": 482.60, "date": 479.85, "license_no": 75.06,
        }

        for field, x in expected.items():
            self.assertAlmostEqual(
                self.layout["fields"][field]["x"], x, delta=2.5, msg=field
            )

    def test_finds_the_item_columns(self):
        columns = self.layout["table"]["columns"]

        for column in ("sr", "name", "qty", "batch", "expiry", "price",
                       "discount", "amount"):
            self.assertIn(column, columns, column)

    def test_numeric_columns_are_right_aligned(self):
        columns = self.layout["table"]["columns"]

        self.assertEqual(columns["amount"]["align"], "right")
        self.assertEqual(columns["name"]["align"], "left")

    def test_finds_the_table_band(self):
        table = self.layout["table"]

        self.assertAlmostEqual(table["header_bottom"], 206.15, delta=1)
        self.assertAlmostEqual(table["bottom"], 240.7, delta=1)

    def test_finds_the_totals(self):
        for total in ("gross", "discount", "net", "company_total"):
            self.assertIn(total, self.layout["totals"], total)

    def test_finds_free_space_for_the_balance_block(self):
        band = self.layout["previous_balance"]

        self.assertIsNotNone(band)
        self.assertGreater(band["bottom"] - band["top"], 100)

    def test_rows_per_page_matches_the_band(self):
        self.assertEqual(rows_per_page(self.layout), 3)

    def test_a_pdf_without_a_table_is_rejected(self):
        """A payslip or a scan must not be accepted as an invoice template."""
        import pymupdf

        document = pymupdf.open()
        page = document.new_page()
        page.insert_text((72, 72), "This is not an invoice.", fontsize=12)

        path = Path(tempfile.mkdtemp()) / "not-an-invoice.pdf"
        document.save(str(path))
        document.close()

        with self.assertRaises(LayoutError):
            detect_layout(str(path))

    def test_an_empty_pdf_is_rejected(self):
        import pymupdf

        document = pymupdf.open()
        document.new_page()

        path = Path(tempfile.mkdtemp()) / "blank.pdf"
        document.save(str(path))
        document.close()

        with self.assertRaises(LayoutError) as caught:
            detect_layout(str(path))

        self.assertIn("No text", str(caught.exception))


class DistributorTests(TestCase):

    def setUp(self):
        User.objects.create_user("clerk", password="pw")
        self.client.login(username="clerk", password="pw")

        self.hhc = Distributor.objects.get(code="HHC")

    def test_seed_migration_registered_the_original_company(self):
        self.assertEqual(self.hhc.name, "HADI HEALTH CARE")
        self.assertTrue(self.hhc.is_default)
        self.assertTrue(self.hhc.template)

    def test_each_distributor_has_its_own_number_series(self):
        other = Distributor.objects.create(
            name="Other Distributor", code="ODC", invoice_start_number=1
        )
        customer = Customer.objects.create(name="Shifa", address="x")

        first = Invoice.objects.create(
            customer=customer, distributor=self.hhc, license_no="L"
        )
        second = Invoice.objects.create(
            customer=customer, distributor=other, license_no="L"
        )
        third = Invoice.objects.create(
            customer=customer, distributor=other, license_no="L"
        )

        self.assertTrue(first.invoice_no.startswith("HHC-"))
        self.assertEqual(second.invoice_no, "ODC-0001")
        self.assertEqual(third.invoice_no, "ODC-0002")

    def test_one_series_does_not_disturb_another(self):
        other = Distributor.objects.create(
            name="Other", code="ODC", invoice_start_number=500
        )
        customer = Customer.objects.create(name="Shifa", address="x")

        Invoice.objects.create(customer=customer, distributor=other, license_no="L")
        hhc_invoice = Invoice.objects.create(
            customer=customer, distributor=self.hhc, license_no="L"
        )

        self.assertEqual(hhc_invoice.invoice_no, "HHC-9965")

    def test_only_one_distributor_can_be_default(self):
        other = Distributor.objects.create(
            name="Other", code="ODC", is_default=True
        )

        self.hhc.refresh_from_db()
        self.assertFalse(self.hhc.is_default)
        self.assertEqual(Distributor.default(), other)

    def test_code_is_uppercased(self):
        distributor = Distributor.objects.create(name="Lower", code="abc")

        self.assertEqual(distributor.code, "ABC")

    def test_duplicate_code_is_rejected_by_the_form(self):
        form = DistributorForm(data={
            "name": "Clashing", "code": "hhc", "address": "", "phone": "",
            "license_no": "", "ntn": "", "sales_tax": "",
            "invoice_start_number": 1, "is_active": True, "is_default": False,
        })

        self.assertFalse(form.is_valid())
        self.assertIn("code", form.errors)

    def test_layout_page_renders(self):
        response = self.client.get(
            reverse("distributor_layout", args=[self.hhc.pk])
        )

        self.assertEqual(response.status_code, 200)

    def test_preview_returns_a_pdf(self):
        if not self.hhc.layout:
            self.hhc.layout = detect_layout(self.hhc.template.path)
            self.hhc.save()

        response = self.client.get(
            reverse("distributor_preview", args=[self.hhc.pk])
        )

        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))


class StockTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user("clerk", password="pw")
        self.client.login(username="clerk", password="pw")

        self.supplier = Supplier.objects.create(name="ABC Pharma")
        self.product = Product.objects.create(
            code="PAN500", name="Panadol 500mg",
            trade_price=Decimal("100.00"), reorder_level=20,
        )

    def make_batch(self, batch_no="B1", quantity=0, days=365):
        return Batch.objects.create(
            product=self.product, batch_no=batch_no,
            expiry_date=timezone.localdate() + timedelta(days=days),
            cost_price=Decimal("80.00"), quantity=quantity,
        )

    def test_receiving_adds_stock_and_logs_a_movement(self):
        batch = self.make_batch()

        receive(batch, 50, reference="GRN-1", user=self.user)

        batch.refresh_from_db()
        self.assertEqual(batch.quantity, 50)
        self.assertEqual(batch.received_quantity, 50)
        self.assertEqual(StockMovement.objects.get().quantity, 50)

    def test_issuing_removes_stock(self):
        batch = self.make_batch(quantity=50)

        issue(batch, 20, reference="HHC-9965", user=self.user)

        batch.refresh_from_db()
        self.assertEqual(batch.quantity, 30)
        self.assertEqual(StockMovement.objects.get().quantity, -20)

    def test_overselling_is_refused(self):
        """Shipping stock that does not exist must never be silent."""
        batch = self.make_batch(quantity=5)

        with self.assertRaises(StockError):
            issue(batch, 6)

        batch.refresh_from_db()
        self.assertEqual(batch.quantity, 5)
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_zero_or_negative_quantities_are_refused(self):
        batch = self.make_batch(quantity=5)

        for bad in (0, -1):
            with self.assertRaises(StockError):
                issue(batch, bad)
            with self.assertRaises(StockError):
                receive(batch, bad)

    def test_adjustment_records_the_difference(self):
        batch = self.make_batch(quantity=50)

        movement = adjust(batch, 45, note="Counted", user=self.user)

        batch.refresh_from_db()
        self.assertEqual(batch.quantity, 45)
        self.assertEqual(movement.quantity, -5)
        self.assertEqual(movement.kind, StockMovement.ADJUSTMENT)

    def test_adjustment_to_the_same_quantity_is_a_no_op(self):
        batch = self.make_batch(quantity=50)

        self.assertIsNone(adjust(batch, 50))
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_expired_stock_is_not_sellable(self):
        self.make_batch("OLD", quantity=10, days=-1)
        self.make_batch("NEW", quantity=7, days=100)

        self.assertEqual(self.product.stock_on_hand, 17)
        self.assertEqual(self.product.sellable_stock, 7)

    def test_fefo_allocates_the_soonest_expiry_first(self):
        later = self.make_batch("LATER", quantity=50, days=500)
        sooner = self.make_batch("SOONER", quantity=30, days=100)

        picks, short = allocate_fefo(self.product, 45)

        self.assertEqual([(b.batch_no, q) for b, q in picks],
                         [("SOONER", 30), ("LATER", 15)])
        self.assertEqual(short, 0)

    def test_fefo_reports_a_shortfall(self):
        self.make_batch("ONLY", quantity=10, days=100)

        picks, short = allocate_fefo(self.product, 25)

        self.assertEqual(short, 15)

    def test_fefo_skips_expired_batches(self):
        self.make_batch("EXPIRED", quantity=100, days=-5)

        picks, short = allocate_fefo(self.product, 10)

        self.assertEqual(picks, [])
        self.assertEqual(short, 10)

    def test_reorder_level_flags_low_stock(self):
        self.make_batch(quantity=15, days=200)

        self.assertTrue(self.product.needs_reorder)

        adjust(self.make_batch("B2", quantity=0, days=200), 30)

        self.assertFalse(Product.objects.get(pk=self.product.pk).needs_reorder)


class PurchaseFlowTests(TestCase):

    def setUp(self):
        User.objects.create_user("clerk", password="pw")
        self.client.login(username="clerk", password="pw")

        self.supplier = Supplier.objects.create(name="ABC Pharma")
        self.product = Product.objects.create(
            code="PAN500", name="Panadol", trade_price=Decimal("100.00")
        )

    def receive_post(self, **overrides):
        payload = {
            "supplier": self.supplier.pk,
            "reference": "SUP-1",
            "date": timezone.localdate().isoformat(),
            "note": "",
            "product[]": [self.product.pk],
            "batch_no[]": ["B-1"],
            "expiry_date[]": [
                (timezone.localdate() + timedelta(days=400)).isoformat()
            ],
            "quantity[]": ["50"],
            "cost_price[]": ["80.00"],
        }
        payload.update(overrides)

        return self.client.post(reverse("purchase_new"), payload)

    def test_receiving_creates_batch_stock_and_movement(self):
        response = self.receive_post()

        self.assertRedirects(response, reverse("purchase_list"))

        batch = Batch.objects.get()
        self.assertEqual(batch.quantity, 50)
        self.assertEqual(batch.batch_no, "B-1")
        self.assertEqual(StockMovement.objects.get().kind, StockMovement.PURCHASE)
        self.assertEqual(PurchaseItem.objects.get().quantity, 50)

    def test_purchase_total_is_quantity_times_cost(self):
        self.receive_post()

        self.assertEqual(Purchase.objects.get().total, Decimal("4000.00"))

    def test_receiving_the_same_batch_again_tops_it_up(self):
        self.receive_post()
        self.receive_post()

        self.assertEqual(Batch.objects.count(), 1)
        self.assertEqual(Batch.objects.get().quantity, 100)

    def test_a_line_without_a_batch_number_is_rejected(self):
        response = self.receive_post(**{"batch_no[]": ["  "]})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Batch.objects.count(), 0)
        self.assertEqual(Purchase.objects.count(), 0)

    def test_a_line_without_an_expiry_is_rejected(self):
        """Pharma stock without an expiry date cannot be tracked safely."""
        response = self.receive_post(**{"expiry_date[]": [""]})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Batch.objects.count(), 0)

    def test_a_zero_quantity_line_is_rejected(self):
        response = self.receive_post(**{"quantity[]": ["0"]})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Purchase.objects.count(), 0)

    def test_nothing_is_written_when_any_line_is_invalid(self):
        """A part-applied delivery note would silently misstate stock."""
        response = self.receive_post(
            **{
                "product[]": [self.product.pk, self.product.pk],
                "batch_no[]": ["GOOD", ""],
                "expiry_date[]": [
                    (timezone.localdate() + timedelta(days=400)).isoformat(),
                    (timezone.localdate() + timedelta(days=400)).isoformat(),
                ],
                "quantity[]": ["10", "10"],
                "cost_price[]": ["80", "80"],
            }
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Batch.objects.count(), 0)
        self.assertEqual(Purchase.objects.count(), 0)


class InvoiceStockTests(TestCase):

    def setUp(self):
        User.objects.create_user("clerk", password="pw")
        self.client.login(username="clerk", password="pw")

        self.product = Product.objects.create(
            code="PAN500", name="Panadol", trade_price=Decimal("100.00")
        )
        self.batch = Batch.objects.create(
            product=self.product, batch_no="B-1",
            expiry_date=timezone.localdate() + timedelta(days=365),
            cost_price=Decimal("80.00"), quantity=50,
        )

    def sell(self, quantity="10", batch_id=None):
        return self.client.post(reverse("generate"), {
            "distributor": Distributor.objects.get(code="HHC").pk,
            "customer_name": "Shifa Pharmacy", "address": "Lahore",
            "ntn": "1", "sales_tax": "2", "license_no": "L",
            "item_name[]": ["Panadol"], "qty[]": [quantity],
            "price[]": ["100"], "discount[]": ["10"],
            "batch[]": [""], "expiry[]": [""],
            "stock_batch[]": [str(self.batch.pk if batch_id is None else batch_id)],
        })

    def test_selling_from_stock_deducts_the_batch(self):
        self.sell("10")

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity, 40)

    def test_sale_is_recorded_in_the_movement_ledger(self):
        self.sell("10")

        movement = StockMovement.objects.get(kind=StockMovement.SALE)
        self.assertEqual(movement.quantity, -10)
        self.assertEqual(movement.reference, Invoice.objects.get().invoice_no)

    def test_item_records_which_batch_it_came_from(self):
        self.sell("10")

        item = Item.objects.get()
        self.assertEqual(item.product, self.product)
        self.assertEqual(item.stock_batch, self.batch)
        self.assertEqual(item.batch, "B-1")

    def test_selling_more_than_stock_does_not_go_negative(self):
        response = self.sell("999")

        self.batch.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.batch.quantity, 50)
        self.assertFalse(StockMovement.objects.filter(kind=StockMovement.SALE).exists())

    def test_free_text_items_still_work_and_move_no_stock(self):
        """Ad-hoc lines must not require a product record."""
        response = self.client.post(reverse("generate"), {
            "customer_name": "Shifa Pharmacy", "address": "Lahore",
            "ntn": "", "sales_tax": "", "license_no": "L",
            "item_name[]": ["Something not in stock"], "qty[]": ["3"],
            "price[]": ["50"], "discount[]": ["0"],
            "batch[]": ["X1"], "expiry[]": ["12/27"], "stock_batch[]": [""],
        })

        self.assertEqual(response["Content-Type"], "application/pdf")
        item = Item.objects.get()
        self.assertIsNone(item.product)
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_batches_endpoint_lists_sellable_stock(self):
        Batch.objects.create(
            product=self.product, batch_no="EXPIRED",
            expiry_date=timezone.localdate() - timedelta(days=1), quantity=99,
        )

        data = self.client.get(
            reverse("product_batches", args=[self.product.pk])
        ).json()

        numbers = [b["batch_no"] for b in data["batches"]]
        self.assertIn("B-1", numbers)
        self.assertNotIn("EXPIRED", numbers)


class TerritoryImportTests(TestCase):
    """The zone breakdown arrives as a spreadsheet, so importing must be
    repeatable without duplicating anything."""

    HEADER = [
        "zone", "territory_code", "territory_name", "city", "call_point",
        "address", "estimated_volume",
    ]

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_csv(self, rows, header=None):
        path = Path(self.tmp) / "territories.csv"

        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(header or self.HEADER)
            writer.writerows(rows)

        return str(path)

    def run_import(self, rows, **kwargs):
        call_command("import_territories", file=self.write_csv(rows),
                     verbosity=0, **kwargs)

    def test_imports_the_shipped_file(self):
        """The real spreadsheet: 16 territories across 4 zones, 32 call points."""
        call_command("import_territories", verbosity=0)

        self.assertEqual(Territory.objects.count(), 16)
        self.assertEqual(CallPoint.objects.count(), 32)
        self.assertEqual(
            sorted({t.region for t in Territory.objects.all()}),
            ["East", "North", "South", "West"],
        )

    def test_territory_carries_its_code_and_zone(self):
        call_command("import_territories", verbosity=0)

        territory = Territory.objects.get(code="N-01")
        self.assertEqual(territory.name, "Mayo Hub")
        self.assertEqual(territory.city, "Lahore")
        self.assertEqual(territory.region, "North")

    def test_call_points_attach_to_their_territory(self):
        call_command("import_territories", verbosity=0)

        mayo = Territory.objects.get(code="N-01")
        names = set(mayo.call_points.values_list("name", flat=True))

        self.assertEqual(len(names), 3)
        self.assertIn("Mayo Hospital", names)

    def test_address_columns_are_joined(self):
        call_command("import_territories", verbosity=0)

        point = CallPoint.objects.get(name="Mayo Hospital")

        self.assertEqual(point.address, "Hospital Rd, Anarkali Bazaar")
        self.assertEqual(point.estimated_volume, "500+")

    def test_kind_is_inferred_from_the_name(self):
        call_command("import_territories", verbosity=0)

        self.assertEqual(
            CallPoint.objects.get(name="Lohari Wholesale Market").kind, "chemist"
        )
        self.assertEqual(
            CallPoint.objects.get(name="Mayo Hospital").kind, "hospital"
        )
        self.assertEqual(
            CallPoint.objects.get(name="Ravi Road GP Clusters").kind, "doctor"
        )

    def test_running_twice_creates_nothing_new(self):
        call_command("import_territories", verbosity=0)
        call_command("import_territories", verbosity=0)

        self.assertEqual(Territory.objects.count(), 16)
        self.assertEqual(CallPoint.objects.count(), 32)

    def test_dry_run_writes_nothing(self):
        call_command("import_territories", dry_run=True, verbosity=0)

        self.assertEqual(Territory.objects.count(), 0)
        self.assertEqual(CallPoint.objects.count(), 0)

    def test_existing_territory_is_updated_not_duplicated(self):
        Territory.objects.create(code="N-01", name="Mayo Hub", city="Lahore")

        call_command("import_territories", verbosity=0)

        self.assertEqual(Territory.objects.filter(code="N-01").count(), 1)
        self.assertEqual(Territory.objects.get(code="N-01").region, "North")

    def test_a_matching_name_without_a_code_is_reused(self):
        """Territories added by hand before the import must not be duplicated."""
        Territory.objects.create(name="Mayo Hub", city="Lahore")

        call_command("import_territories", verbosity=0)

        territory = Territory.objects.get(name="Mayo Hub")
        self.assertEqual(territory.code, "N-01")
        self.assertEqual(Territory.objects.filter(name="Mayo Hub").count(), 1)

    def test_updated_address_is_applied_on_reimport(self):
        self.run_import([["North", "N-01", "Mayo Hub", "Lahore",
                          "Mayo Hospital", "Old Road", "100+"]])
        self.run_import([["North", "N-01", "Mayo Hub", "Lahore",
                          "Mayo Hospital", "New Road", "500+"]])

        point = CallPoint.objects.get(name="Mayo Hospital")
        self.assertEqual(point.address, "New Road")
        self.assertEqual(point.estimated_volume, "500+")

    def test_same_name_in_two_territories_is_kept_separate(self):
        self.run_import([
            ["North", "N-01", "Mayo Hub", "Lahore", "City Clinic", "A", "10+"],
            ["South", "S-01", "Johar Town", "Lahore", "City Clinic", "B", "20+"],
        ])

        self.assertEqual(CallPoint.objects.filter(name="City Clinic").count(), 2)

    def test_rows_without_a_call_point_are_skipped(self):
        self.run_import([["North", "N-01", "Mayo Hub", "Lahore", "", "", ""]])

        self.assertEqual(Territory.objects.count(), 1)
        self.assertEqual(CallPoint.objects.count(), 0)

    def test_a_file_missing_columns_is_rejected(self):
        path = self.write_csv([["North", "Mayo Hub"]], header=["zone", "name"])

        with self.assertRaises(CommandError) as caught:
            call_command("import_territories", file=path, verbosity=0)

        self.assertIn("Missing column", str(caught.exception))

    def test_a_missing_file_is_rejected(self):
        with self.assertRaises(CommandError):
            call_command("import_territories", file="/nope/absent.csv", verbosity=0)

    def test_imported_territories_drive_plan_generation(self):
        """The point of importing: MRs can be planned against real call points."""
        call_command("import_territories", verbosity=0)

        territory = Territory.objects.get(code="S-03")
        mr = Employee.objects.create(
            employee_code="MR-01", full_name="Ali Raza",
            designation="mr", territory=territory,
        )

        plan, created = generate_plan(mr, timezone.localdate())

        self.assertEqual(created, 3)
        self.assertEqual(plan.visit_count, 3)


class ManufacturerTests(TestCase):
    """Manufacturers are who makes a product, distinct from the supplier who
    sells it to us - the same maker often arrives via several suppliers."""

    def setUp(self):
        User.objects.create_user("clerk", password="pw")
        self.client.login(username="clerk", password="pw")

        self.maker = Manufacturer.objects.create(
            name="Getz Pharma", code="GETZ", country="Pakistan",
            drug_licence="DML-0099", contact_person="Mr Khan",
            phone="021-1234567",
        )

    def test_product_records_its_manufacturer(self):
        product = Product.objects.create(
            code="PAN500", name="Panadol", manufacturer=self.maker,
            generic_name="Paracetamol 500mg", registration_no="DRAP-123",
        )

        self.assertEqual(product.manufacturer, self.maker)
        self.assertEqual(self.maker.product_count, 1)

    def test_manufacturer_is_optional(self):
        """Products imported before manufacturers were tracked must still save."""
        product = Product.objects.create(code="X1", name="Unknown Origin")

        self.assertIsNone(product.manufacturer)

    def test_stock_rolls_up_to_the_manufacturer(self):
        product = Product.objects.create(
            code="PAN500", name="Panadol", manufacturer=self.maker
        )
        Batch.objects.create(
            product=product, batch_no="B1", quantity=40,
            expiry_date=timezone.localdate() + timedelta(days=300),
        )
        other = Product.objects.create(
            code="BRU400", name="Brufen", manufacturer=self.maker
        )
        Batch.objects.create(
            product=other, batch_no="B2", quantity=10,
            expiry_date=timezone.localdate() + timedelta(days=300),
        )

        self.assertEqual(self.maker.stock_on_hand, 50)

    def test_a_manufacturer_with_products_cannot_be_deleted(self):
        """Deleting one would orphan its products' provenance."""
        Product.objects.create(code="P1", name="P", manufacturer=self.maker)

        with self.assertRaises(ProtectedError):
            self.maker.delete()

    def test_duplicate_name_is_rejected(self):
        form = ManufacturerForm(data={
            "name": "getz pharma", "code": "G2", "contact_person": "",
            "phone": "", "email": "", "website": "", "address": "",
            "country": "Pakistan", "drug_licence": "", "ntn": "",
            "note": "", "is_active": True,
        })

        self.assertFalse(form.is_valid())
        self.assertIn("name", form.errors)

    def test_detail_page_lists_the_products(self):
        Product.objects.create(
            code="PAN500", name="Panadol", manufacturer=self.maker
        )

        html = self.client.get(
            reverse("manufacturer_detail", args=[self.maker.pk])
        ).content.decode()

        self.assertIn("Panadol", html)
        self.assertIn("DML-0099", html)

    def test_product_list_can_filter_by_manufacturer(self):
        other = Manufacturer.objects.create(name="Abbott")
        Product.objects.create(code="A", name="Getz Product", manufacturer=self.maker)
        Product.objects.create(code="B", name="Abbott Product", manufacturer=other)

        html = self.client.get(
            reverse("product_list"), {"manufacturer": self.maker.pk}
        ).content.decode()

        self.assertIn("Getz Product", html)
        self.assertNotIn("Abbott Product", html)

    def test_products_are_searchable_by_manufacturer_name(self):
        Product.objects.create(code="A", name="Some Tablet", manufacturer=self.maker)

        html = self.client.get(
            reverse("product_list"), {"q": "Getz"}
        ).content.decode()

        self.assertIn("Some Tablet", html)

    def test_global_search_finds_products_by_generic_name(self):
        Product.objects.create(
            code="PAN500", name="Panadol", generic_name="Paracetamol 500mg",
            manufacturer=self.maker,
        )

        html = self.client.get(
            reverse("search"), {"q": "Paracetamol"}
        ).content.decode()

        self.assertIn("Panadol", html)

    def test_creating_one_redirects_to_its_page(self):
        response = self.client.post(reverse("manufacturer_new"), {
            "name": "Searle", "code": "SRL", "contact_person": "",
            "phone": "", "email": "", "website": "", "address": "",
            "country": "Pakistan", "drug_licence": "", "ntn": "",
            "note": "", "is_active": True,
        })

        created = Manufacturer.objects.get(name="Searle")
        self.assertRedirects(
            response, reverse("manufacturer_detail", args=[created.pk])
        )

    def test_manufacturer_pages_render(self):
        for url in (reverse("manufacturer_list"), reverse("manufacturer_new"),
                    reverse("manufacturer_detail", args=[self.maker.pk]),
                    reverse("manufacturer_edit", args=[self.maker.pk])):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)


class DeleteInvoiceDataTests(TestCase):
    """Removing a test invoice must also put back the stock it took out,
    or the ledger is corrected while stock is quietly left wrong."""

    def setUp(self):
        self.user = User.objects.create_user("clerk", password="pw")
        self.client.login(username="clerk", password="pw")

        self.customer = Customer.objects.create(name="Test Pharmacy", address="x")
        self.product = Product.objects.create(
            code="PAN500", name="Panadol", trade_price=Decimal("100.00")
        )
        self.batch = Batch.objects.create(
            product=self.product, batch_no="B-1", quantity=50,
            expiry_date=timezone.localdate() + timedelta(days=365),
        )

    def make_invoice(self, from_stock=True, qty=10):
        invoice = Invoice.objects.create(
            customer=self.customer, license_no="L", total=Decimal("900.00")
        )
        Item.objects.create(
            invoice=invoice, name="Panadol", qty=qty,
            price=Decimal("100.00"), discount=Decimal("10.00"),
            product=self.product if from_stock else None,
            stock_batch=self.batch if from_stock else None,
        )
        InvoiceLog.objects.create(
            invoice=invoice, user=self.user,
            customer_name=self.customer.name, amount=invoice.total,
        )

        if from_stock:
            issue(self.batch, qty, reference=invoice.invoice_no)

        return invoice

    def test_dry_run_deletes_nothing(self):
        self.make_invoice()

        call_command("delete_invoice_data", customer=self.customer.pk, verbosity=0)

        self.assertEqual(Invoice.objects.count(), 1)
        self.assertEqual(Customer.objects.count(), 1)

    def test_confirm_removes_the_invoice_and_its_lines(self):
        self.make_invoice()

        call_command("delete_invoice_data", customer=self.customer.pk,
                     confirm=True, verbosity=0)

        self.assertEqual(Invoice.objects.count(), 0)
        self.assertEqual(Item.objects.count(), 0)
        self.assertEqual(InvoiceLog.objects.count(), 0)
        self.assertEqual(Customer.objects.count(), 0)

    def test_stock_is_returned(self):
        self.make_invoice(qty=10)
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity, 40)

        call_command("delete_invoice_data", customer=self.customer.pk,
                     confirm=True, verbosity=0)

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity, 50)

    def test_the_return_is_recorded_in_the_ledger(self):
        invoice = self.make_invoice(qty=10)
        number = invoice.invoice_no

        call_command("delete_invoice_data", customer=self.customer.pk,
                     confirm=True, verbosity=0)

        movement = StockMovement.objects.filter(
            kind=StockMovement.ADJUSTMENT
        ).latest("id")

        self.assertEqual(movement.quantity, 10)
        self.assertIn(number, movement.note)

    def test_free_text_invoices_touch_no_stock(self):
        self.make_invoice(from_stock=False)

        call_command("delete_invoice_data", customer=self.customer.pk,
                     confirm=True, verbosity=0)

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity, 50)

    def test_payments_are_removed_too(self):
        invoice = self.make_invoice()
        Payment.objects.create(
            customer=self.customer, invoice=invoice, amount=Decimal("100.00")
        )

        call_command("delete_invoice_data", customer=self.customer.pk,
                     confirm=True, verbosity=0)

        self.assertEqual(Payment.objects.count(), 0)

    def test_customer_can_be_kept(self):
        self.make_invoice()

        call_command("delete_invoice_data", customer=self.customer.pk,
                     confirm=True, keep_customer=True, verbosity=0)

        self.assertEqual(Invoice.objects.count(), 0)
        self.assertEqual(Customer.objects.count(), 1)

    def test_a_single_invoice_can_be_targeted(self):
        first = self.make_invoice(qty=5)
        second = self.make_invoice(qty=5)

        call_command("delete_invoice_data", invoice=[first.invoice_no],
                     confirm=True, verbosity=0)

        self.assertEqual(
            list(Invoice.objects.values_list("invoice_no", flat=True)),
            [second.invoice_no],
        )
        self.assertEqual(Customer.objects.count(), 1)

    def test_an_unknown_invoice_number_is_rejected(self):
        with self.assertRaises(CommandError):
            call_command("delete_invoice_data", invoice=["HHC-0000"],
                         confirm=True, verbosity=0)

    def test_an_unknown_customer_is_rejected(self):
        with self.assertRaises(CommandError):
            call_command("delete_invoice_data", customer=999999, verbosity=0)

    def test_calling_with_no_target_is_rejected(self):
        with self.assertRaises(CommandError):
            call_command("delete_invoice_data", verbosity=0)

    def test_numbering_continues_after_a_deletion(self):
        """Deleting the last invoice must not let its number be reissued."""
        distributor = Distributor.objects.get(code="HHC")
        invoice = Invoice.objects.create(
            customer=self.customer, distributor=distributor, license_no="L"
        )
        first_number = invoice.invoice_no

        call_command("delete_invoice_data", invoice=[first_number],
                     confirm=True, verbosity=0)

        replacement = Invoice.objects.create(
            customer=self.customer, distributor=distributor, license_no="L"
        )

        # The series restarts once the table is empty again - flagged rather
        # than silently reissued, since the deleted invoice was never sent.
        self.assertEqual(replacement.invoice_no, first_number)


class SalesReturnTests(TestCase):
    """A return credits the ledger and restocks the goods, without deleting
    the invoice - the customer still has their copy of it."""

    def setUp(self):
        self.user = User.objects.create_user("clerk", password="pw")
        self.client.login(username="clerk", password="pw")

        self.customer = Customer.objects.create(name="Shifa Pharmacy", address="x")
        self.product = Product.objects.create(
            code="PAN500", name="Panadol", trade_price=Decimal("100.00")
        )
        self.batch = Batch.objects.create(
            product=self.product, batch_no="B-1", quantity=50,
            expiry_date=timezone.localdate() + timedelta(days=365),
        )
        self.invoice = Invoice.objects.create(
            customer=self.customer, license_no="L", total=Decimal("900.00")
        )
        self.item = Item.objects.create(
            invoice=self.invoice, name="Panadol", qty=10,
            price=Decimal("100.00"), discount=Decimal("10.00"),
            product=self.product, stock_batch=self.batch,
        )
        issue(self.batch, 10, reference=self.invoice.invoice_no)

    def post_return(self, qty=10, restock=True, **extra):
        payload = {
            f"qty_{self.item.pk}": str(qty),
            "date": timezone.localdate().isoformat(),
            "reason": "Damaged in transit",
        }

        if restock:
            payload["restock"] = "on"

        payload.update(extra)

        return self.client.post(
            reverse("return_create", args=[self.invoice.pk]), payload
        )

    def test_return_credits_the_customer_ledger(self):
        self.post_return(10)

        self.assertEqual(self.customer.total_returned, Decimal("900.00"))
        self.assertEqual(self.customer.outstanding_balance, Decimal("0.00"))

    def test_return_restocks_the_batch(self):
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity, 40)

        self.post_return(10)

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity, 50)

    def test_the_invoice_is_not_deleted(self):
        self.post_return(10)

        self.assertTrue(Invoice.objects.filter(pk=self.invoice.pk).exists())
        self.assertEqual(Invoice.objects.get(pk=self.invoice.pk).total,
                         Decimal("900.00"))

    def test_invoice_balance_drops_to_zero(self):
        self.post_return(10)

        invoice = Invoice.objects.get(pk=self.invoice.pk)
        self.assertEqual(invoice.amount_returned, Decimal("900.00"))
        self.assertEqual(invoice.balance, Decimal("0.00"))
        self.assertTrue(invoice.is_paid)

    def test_partial_return_credits_only_that_part(self):
        self.post_return(4)          # 4 x (100 - 10%) = 360

        invoice = Invoice.objects.get(pk=self.invoice.pk)
        self.assertEqual(invoice.amount_returned, Decimal("360.00"))
        self.assertEqual(invoice.balance, Decimal("540.00"))

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity, 44)

    def test_returning_more_than_sold_is_refused(self):
        response = self.post_return(11)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(SalesReturn.objects.count(), 0)

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity, 40)

    def test_second_return_cannot_exceed_the_remainder(self):
        self.post_return(6)
        response = self.post_return(6)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(SalesReturn.objects.count(), 1)

    def test_two_partial_returns_add_up(self):
        self.post_return(4)
        self.post_return(6)

        invoice = Invoice.objects.get(pk=self.invoice.pk)
        self.assertEqual(invoice.amount_returned, Decimal("900.00"))

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity, 50)

    def test_damaged_goods_are_credited_but_not_restocked(self):
        """Expired or damaged stock must never go back on the shelf."""
        self.post_return(10, restock=False)

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity, 40)
        self.assertEqual(self.customer.total_returned, Decimal("900.00"))

    def test_the_restock_is_recorded_in_the_stock_ledger(self):
        self.post_return(10)

        movement = StockMovement.objects.get(kind=StockMovement.RETURN)
        self.assertEqual(movement.quantity, 10)
        self.assertIn(self.invoice.invoice_no, movement.note)

    def test_return_appears_on_the_statement_as_a_credit(self):
        self.post_return(10)

        response = self.client.get(
            reverse("customer_ledger", args=[self.customer.pk])
        )
        kinds = [entry["kind"] for entry in response.context["entries"]]

        self.assertIn("return", kinds)
        self.assertEqual(response.context["balance"], Decimal("0.00"))

    def test_ledger_list_totals_account_for_credits(self):
        self.post_return(4)

        row = customers_with_balances().get(pk=self.customer.pk)

        self.assertEqual(row.invoiced, Decimal("900.00"))
        self.assertEqual(row.returned, Decimal("360.00"))
        self.assertEqual(row.invoiced - row.paid - row.returned, Decimal("540.00"))

    def test_a_fully_returned_invoice_is_not_overdue(self):
        Invoice.objects.filter(pk=self.invoice.pk).update(
            date=timezone.localdate() - timedelta(days=OVERDUE_DAYS + 5)
        )
        self.assertIn(
            self.invoice.invoice_no,
            [i.invoice_no for i in overdue_invoices()],
        )

        self.post_return(10)

        self.assertNotIn(
            self.invoice.invoice_no,
            [i.invoice_no for i in overdue_invoices()],
        )

    def test_credit_notes_are_numbered_in_sequence(self):
        self.post_return(4)
        self.post_return(6)

        numbers = sorted(SalesReturn.objects.values_list("return_no", flat=True))
        self.assertEqual(numbers, ["CN-0001", "CN-0002"])

    def test_an_empty_return_is_rejected(self):
        response = self.client.post(
            reverse("return_create", args=[self.invoice.pk]),
            {"date": timezone.localdate().isoformat(), "restock": "on"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(SalesReturn.objects.count(), 0)

    def test_return_pages_render(self):
        self.post_return(2)

        for url in (reverse("return_list"),
                    reverse("return_create", args=[self.invoice.pk])):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)


class PurchaseEditTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user("clerk", password="pw")
        self.client.login(username="clerk", password="pw")

        self.supplier = Supplier.objects.create(name="ABC Pharma")
        self.product = Product.objects.create(code="PAN500", name="Panadol")

        self.client.post(reverse("purchase_new"), {
            "supplier": self.supplier.pk, "reference": "SUP-1",
            "date": timezone.localdate().isoformat(), "note": "",
            "product[]": [self.product.pk], "batch_no[]": ["B-1"],
            "expiry_date[]": [
                (timezone.localdate() + timedelta(days=400)).isoformat()
            ],
            "quantity[]": ["50"], "cost_price[]": ["80.00"],
        })

        self.purchase = Purchase.objects.get()
        self.line = PurchaseItem.objects.get()
        self.batch = Batch.objects.get()

    def edit(self, qty=None, cost=None):
        return self.client.post(
            reverse("purchase_edit", args=[self.purchase.pk]),
            {
                "supplier": self.supplier.pk,
                "reference": self.purchase.reference,
                "date": self.purchase.date.isoformat(),
                "note": "",
                # `or` would swallow a deliberate 0, which is the case under test
                f"qty_{self.line.pk}": str(
                    self.line.quantity if qty is None else qty
                ),
                f"cost_{self.line.pk}": str(
                    self.line.cost_price if cost is None else cost
                ),
            },
        )

    def test_cost_price_can_be_corrected(self):
        response = self.edit(cost="95.50")

        self.assertRedirects(response, reverse("purchase_list"))
        self.line.refresh_from_db()
        self.assertEqual(self.line.cost_price, Decimal("95.50"))

    def test_correcting_the_cost_restates_the_batch(self):
        self.edit(cost="95.50")

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.cost_price, Decimal("95.50"))

    def test_purchase_total_follows_the_new_cost(self):
        self.edit(cost="100.00")

        self.assertEqual(Purchase.objects.get().total, Decimal("5000.00"))

    def test_increasing_the_quantity_adds_stock(self):
        self.edit(qty=60)

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity, 60)
        self.assertEqual(self.batch.received_quantity, 60)

    def test_decreasing_the_quantity_removes_stock(self):
        self.edit(qty=30)

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity, 30)

    def test_the_correction_is_recorded_in_the_ledger(self):
        self.edit(qty=60)

        movement = StockMovement.objects.filter(
            kind=StockMovement.ADJUSTMENT
        ).latest("id")

        self.assertEqual(movement.quantity, 10)
        self.assertIn("corrected", movement.note)

    def test_cannot_reduce_below_what_has_been_sold(self):
        """40 of the 50 have gone out; cutting the receipt to 5 is impossible."""
        issue(self.batch, 40, reference="HHC-9965")

        response = self.edit(qty=5)

        self.assertEqual(response.status_code, 200)
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity, 10)
        self.assertEqual(PurchaseItem.objects.get().quantity, 50)

    def test_zero_quantity_is_rejected(self):
        response = self.edit(qty=0)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(PurchaseItem.objects.get().quantity, 50)

    def test_editing_nothing_changes_nothing(self):
        before = StockMovement.objects.count()

        self.edit()

        self.assertEqual(StockMovement.objects.count(), before)


class StockLedgerTests(TestCase):

    def setUp(self):
        User.objects.create_user("clerk", password="pw")
        self.client.login(username="clerk", password="pw")

        self.product = Product.objects.create(code="PAN500", name="Panadol")
        self.batch = Batch.objects.create(
            product=self.product, batch_no="B-1", quantity=0,
            expiry_date=timezone.localdate() + timedelta(days=365),
        )

    def test_running_balance_tracks_each_movement(self):
        receive(self.batch, 100, reference="GRN-1")
        issue(self.batch, 30, reference="HHC-9965")
        issue(self.batch, 20, reference="HHC-9966")

        entries = self.client.get(reverse("stock_ledger")).context["entries"]

        # Newest first, so the top row carries the closing balance.
        self.assertEqual(entries[0]["balance"], 50)
        self.assertEqual([e["balance"] for e in entries], [50, 70, 100])

    def test_totals_split_in_and_out(self):
        receive(self.batch, 100)
        issue(self.batch, 30)

        response = self.client.get(reverse("stock_ledger"))

        self.assertEqual(response.context["total_in"], 100)
        self.assertEqual(response.context["total_out"], 30)

    def test_balances_are_kept_per_batch(self):
        other = Batch.objects.create(
            product=self.product, batch_no="B-2", quantity=0,
            expiry_date=timezone.localdate() + timedelta(days=365),
        )
        receive(self.batch, 10)
        receive(other, 5)

        entries = self.client.get(reverse("stock_ledger")).context["entries"]
        balances = {e["movement"].batch.batch_no: e["balance"] for e in entries}

        self.assertEqual(balances, {"B-1": 10, "B-2": 5})

    def test_can_be_filtered_to_one_product(self):
        other_product = Product.objects.create(code="BRU", name="Brufen")
        other_batch = Batch.objects.create(
            product=other_product, batch_no="X", quantity=0,
            expiry_date=timezone.localdate() + timedelta(days=365),
        )
        receive(self.batch, 10)
        receive(other_batch, 7)

        entries = self.client.get(
            reverse("stock_ledger"), {"product": self.product.pk}
        ).context["entries"]

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["movement"].product, self.product)


class ExpenseTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user("clerk", password="pw")
        self.client.login(username="clerk", password="pw")

        self.employee = Employee.objects.create(
            employee_code="MR-01", full_name="Ali Raza", designation="mr"
        )
        self.fuel = ExpenseCategory.objects.get(name="Fuel Allowance")
        self.drap = ExpenseCategory.objects.get(name="DRAP Fees")

    def test_the_categories_you_asked_for_are_seeded(self):
        names = set(ExpenseCategory.objects.values_list("name", flat=True))

        for expected in ("Fuel Allowance", "Doctor Refreshment",
                         "Literature Expense", "Promotional Material",
                         "DRAP Fees", "Salary & Payroll"):
            self.assertIn(expected, names)

    def test_claim_is_recorded_against_a_team_member(self):
        response = self.client.post(reverse("expense_new"), {
            "category": self.fuel.pk, "employee": self.employee.pk,
            "territory": "", "date": timezone.localdate().isoformat(),
            "amount": "4500.00", "description": "Petrol", "reference": "B-1",
        })

        self.assertRedirects(response, reverse("expense_list"))
        expense = Expense.objects.get()
        self.assertEqual(expense.employee, self.employee)
        self.assertEqual(expense.status, Expense.PENDING)
        self.assertEqual(expense.submitted_by, self.user)

    def test_a_per_employee_category_requires_someone_to_claim_it(self):
        """A fuel allowance with nobody attached cannot be reported per person."""
        form = ExpenseForm(data={
            "category": self.fuel.pk, "employee": "", "territory": "",
            "date": timezone.localdate().isoformat(), "amount": "1000",
            "description": "", "reference": "",
        })

        self.assertFalse(form.is_valid())
        self.assertIn("employee", form.errors)

    def test_company_costs_need_no_employee(self):
        form = ExpenseForm(data={
            "category": self.drap.pk, "employee": "", "territory": "",
            "date": timezone.localdate().isoformat(), "amount": "25000",
            "description": "Renewal", "reference": "",
        })

        self.assertTrue(form.is_valid(), form.errors)

    def test_zero_amount_is_rejected(self):
        form = ExpenseForm(data={
            "category": self.drap.pk, "employee": "", "territory": "",
            "date": timezone.localdate().isoformat(), "amount": "0",
            "description": "", "reference": "",
        })

        self.assertFalse(form.is_valid())

    def make_expense(self, amount="1000", status=Expense.PENDING, employee=True):
        return Expense.objects.create(
            category=self.fuel if employee else self.drap,
            employee=self.employee if employee else None,
            date=timezone.localdate(), amount=Decimal(amount),
            status=status, submitted_by=self.user,
        )

    def test_approving_records_who_and_when(self):
        expense = self.make_expense()

        self.client.post(reverse("expense_status", args=[expense.pk, "approve"]))

        expense.refresh_from_db()
        self.assertEqual(expense.status, Expense.APPROVED)
        self.assertEqual(expense.reviewed_by, self.user)
        self.assertIsNotNone(expense.reviewed_at)

    def test_rejected_claims_are_not_counted_as_spend(self):
        self.make_expense("1000", Expense.APPROVED)
        self.make_expense("9999", Expense.REJECTED)

        response = self.client.get(reverse("expense_list"))

        self.assertEqual(response.context["total"], Decimal("1000.00"))

    def test_report_totals_by_category_and_by_person(self):
        self.make_expense("1000", Expense.APPROVED)
        self.make_expense("25000", Expense.APPROVED, employee=False)

        response = self.client.get(reverse("expense_report"))

        by_category = dict(response.context["by_category"])
        by_employee = dict(response.context["by_employee"])

        self.assertEqual(by_category["Fuel Allowance"], Decimal("1000.00"))
        self.assertEqual(by_category["DRAP Fees"], Decimal("25000.00"))
        self.assertEqual(by_employee["Ali Raza"], Decimal("1000.00"))
        self.assertEqual(by_employee["Company"], Decimal("25000.00"))

    def test_list_can_be_filtered_by_employee(self):
        other = Employee.objects.create(employee_code="MR-02", full_name="Sara")
        self.make_expense("1000")
        Expense.objects.create(
            category=self.fuel, employee=other, date=timezone.localdate(),
            amount=Decimal("500"), submitted_by=self.user,
        )

        response = self.client.get(
            reverse("expense_list"), {"employee": self.employee.pk}
        )

        self.assertEqual(len(response.context["expenses"]), 1)

    def test_expense_pages_render(self):
        self.make_expense()

        for url in (reverse("expense_list"), reverse("expense_new"),
                    reverse("expense_report"),
                    reverse("expense_category_list")):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)


class SamplingTests(TestCase):
    """Samples come out of the same stock that gets sold, so 2500 packs stay
    2500 packs across sales and samples together."""

    def setUp(self):
        self.user = User.objects.create_user("clerk", password="pw")
        self.client.login(username="clerk", password="pw")

        self.territory = Territory.objects.create(name="Gulberg", city="Lahore")
        self.mr = Employee.objects.create(
            employee_code="MR-01", full_name="Ali Raza",
            designation="mr", territory=self.territory,
        )
        self.doctor = CallPoint.objects.create(
            name="Dr. Ahmed", territory=self.territory, kind="doctor"
        )
        self.product = Product.objects.create(code="PAN500", name="Panadol")
        self.batch = Batch.objects.create(
            product=self.product, batch_no="B-1", quantity=2500,
            cost_price=Decimal("20.00"),
            expiry_date=timezone.localdate() + timedelta(days=365),
        )

    def issue_samples(self, qty=10, batch=None):
        return self.client.post(reverse("sample_new"), {
            "employee": self.mr.pk,
            "call_point": self.doctor.pk,
            "date": timezone.localdate().isoformat(),
            "note": "",
            "batch[]": [str((batch or self.batch).pk)],
            "qty[]": [str(qty)],
        })

    def test_sampling_reduces_stock(self):
        self.issue_samples(10)

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity, 2490)

    def test_sample_is_recorded_with_its_own_movement_kind(self):
        self.issue_samples(10)

        movement = StockMovement.objects.get(kind=StockMovement.SAMPLE)
        self.assertEqual(movement.quantity, -10)
        self.assertIn("Dr. Ahmed", movement.note)

    def test_issue_records_who_gave_what_to_whom(self):
        self.issue_samples(10)

        issue_record = SampleIssue.objects.get()
        self.assertEqual(issue_record.employee, self.mr)
        self.assertEqual(issue_record.call_point, self.doctor)
        self.assertEqual(issue_record.total_units, 10)
        self.assertEqual(issue_record.total_value, Decimal("200.00"))

    def test_references_are_sequential(self):
        self.issue_samples(1)
        self.issue_samples(1)

        self.assertEqual(
            sorted(SampleIssue.objects.values_list("reference", flat=True)),
            ["SMP-0001", "SMP-0002"],
        )

    def test_cannot_sample_more_than_is_in_stock(self):
        response = self.issue_samples(3000)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(SampleIssue.objects.count(), 0)

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity, 2500)

    def test_two_lines_of_the_same_batch_are_checked_together(self):
        """Each line fits alone, but their sum does not."""
        response = self.client.post(reverse("sample_new"), {
            "employee": self.mr.pk, "call_point": self.doctor.pk,
            "date": timezone.localdate().isoformat(), "note": "",
            "batch[]": [str(self.batch.pk), str(self.batch.pk)],
            "qty[]": ["1500", "1500"],
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(SampleIssue.objects.count(), 0)
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity, 2500)

    def test_expired_stock_cannot_be_sampled(self):
        expired = Batch.objects.create(
            product=self.product, batch_no="OLD", quantity=100,
            expiry_date=timezone.localdate() - timedelta(days=1),
        )

        response = self.issue_samples(5, batch=expired)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(SampleIssue.objects.count(), 0)

    def test_samples_and_sales_draw_on_the_same_stock(self):
        issue(self.batch, 500, reference="HHC-9965")
        self.issue_samples(100)

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity, 1900)

    def test_report_breaks_samples_down_three_ways(self):
        self.issue_samples(10)

        response = self.client.get(reverse("sample_report"))

        self.assertEqual(dict(response.context["by_employee"])["Ali Raza"], 10)
        self.assertEqual(dict(response.context["by_product"])["Panadol"], 10)
        self.assertEqual(dict(response.context["by_doctor"])["Dr. Ahmed"], 10)

    def test_sampling_pages_render(self):
        self.issue_samples(5)

        for url in (reverse("sample_list"), reverse("sample_new"),
                    reverse("sample_report")):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)


class PayrollTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user("clerk", password="pw")
        self.client.login(username="clerk", password="pw")

        self.employee = Employee.objects.create(
            employee_code="MR-01", full_name="Ali Raza", designation="mr",
            basic_salary=Decimal("60000"), fuel_allowance=Decimal("15000"),
            mobile_allowance=Decimal("2000"),
        )

    def run_payroll(self, month=None):
        return self.client.post(reverse("payroll_create"), {
            "month": (month or timezone.localdate()).isoformat(), "note": "",
        })

    def test_a_slip_is_generated_for_every_active_employee(self):
        Employee.objects.create(
            employee_code="MR-02", full_name="Sara", basic_salary=Decimal("50000")
        )
        Employee.objects.create(
            employee_code="MR-03", full_name="Gone", is_active=False
        )

        self.run_payroll()

        self.assertEqual(Payslip.objects.count(), 2)

    def test_gross_and_net_are_calculated(self):
        self.run_payroll()

        slip = Payslip.objects.get()
        self.assertEqual(slip.gross_pay, Decimal("77000.00"))
        self.assertEqual(slip.net_pay, Decimal("77000.00"))

    def test_approved_expenses_are_reimbursed_with_salary(self):
        fuel = ExpenseCategory.objects.get(name="Fuel Allowance")
        Expense.objects.create(
            category=fuel, employee=self.employee, date=timezone.localdate(),
            amount=Decimal("4500"), status=Expense.APPROVED,
            submitted_by=self.user,
        )

        self.run_payroll()

        slip = Payslip.objects.get()
        self.assertEqual(slip.expense_reimbursement, Decimal("4500.00"))
        self.assertEqual(slip.gross_pay, Decimal("81500.00"))

    def test_unapproved_expenses_are_not_reimbursed(self):
        fuel = ExpenseCategory.objects.get(name="Fuel Allowance")
        Expense.objects.create(
            category=fuel, employee=self.employee, date=timezone.localdate(),
            amount=Decimal("9999"), status=Expense.PENDING,
            submitted_by=self.user,
        )

        self.run_payroll()

        self.assertEqual(Payslip.objects.get().expense_reimbursement, ZERO)

    def test_deductions_reduce_net_pay(self):
        self.run_payroll()
        slip = Payslip.objects.get()

        self.client.post(reverse("payslip_edit", args=[slip.pk]), {
            "basic_salary": "60000", "fuel_allowance": "15000",
            "mobile_allowance": "2000", "other_allowance": "0",
            "expense_reimbursement": "0", "tax_deduction": "5000",
            "advance_deduction": "2000", "other_deduction": "0", "note": "",
        })

        slip.refresh_from_db()
        self.assertEqual(slip.total_deductions, Decimal("7000.00"))
        self.assertEqual(slip.net_pay, Decimal("70000.00"))

    def test_a_slip_snapshots_pay_so_later_raises_do_not_rewrite_it(self):
        self.run_payroll()

        self.employee.basic_salary = Decimal("90000")
        self.employee.save()

        self.assertEqual(Payslip.objects.get().basic_salary, Decimal("60000.00"))

    def test_only_one_run_per_month(self):
        self.run_payroll()
        self.run_payroll()

        self.assertEqual(PayrollRun.objects.count(), 1)

    def test_the_month_is_stored_as_the_first(self):
        self.run_payroll(date(2026, 8, 17))

        self.assertEqual(PayrollRun.objects.get().month, date(2026, 8, 1))

    def test_a_finalised_run_cannot_be_edited(self):
        self.run_payroll()
        run = PayrollRun.objects.get()
        slip = Payslip.objects.get()

        self.client.post(reverse("payroll_finalise", args=[run.pk]))

        response = self.client.post(reverse("payslip_edit", args=[slip.pk]), {
            "basic_salary": "1", "fuel_allowance": "0", "mobile_allowance": "0",
            "other_allowance": "0", "expense_reimbursement": "0",
            "tax_deduction": "0", "advance_deduction": "0",
            "other_deduction": "0", "note": "",
        })

        self.assertRedirects(response, reverse("payroll_detail", args=[run.pk]))
        self.assertEqual(Payslip.objects.get().basic_salary, Decimal("60000.00"))

    def test_payslip_pdf_carries_the_details_and_the_logo(self):
        import pymupdf

        self.run_payroll()
        slip = Payslip.objects.get()

        response = self.client.get(reverse("payslip_pdf", args=[slip.pk]))

        self.assertEqual(response["Content-Type"], "application/pdf")

        document = pymupdf.open(stream=response.content, filetype="pdf")
        text = document[0].get_text()

        self.assertIn("SALARY SLIP", text)
        self.assertIn("Ali Raza", text)
        self.assertIn("MR-01", text)
        self.assertIn("NET PAY", text)
        self.assertIn("77,000.00", text)
        # The LifeMed logo is embedded, not just named
        self.assertEqual(len(document[0].get_images()), 1)

    def test_payroll_pages_render(self):
        self.run_payroll()
        run = PayrollRun.objects.get()
        slip = Payslip.objects.get()

        for url in (reverse("payroll_list"),
                    reverse("payroll_detail", args=[run.pk]),
                    reverse("payslip_edit", args=[slip.pk])):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)


class CallReportTests(TestCase):
    """An MR opens the day, sees the schedule, and logs who was actually seen."""

    def setUp(self):
        self.user = User.objects.create_user("ali", password="pw")
        self.client.login(username="ali", password="pw")

        self.territory = Territory.objects.create(name="Gulberg", city="Lahore")
        self.mr = Employee.objects.create(
            employee_code="MR-01", full_name="Ali Raza", designation="mr",
            territory=self.territory, user=self.user,
        )
        self.doctor = CallPoint.objects.create(
            name="Dr. Ahmed", territory=self.territory,
            kind="doctor", speciality="Cardiology",
        )
        self.product = Product.objects.create(code="PAN500", name="Panadol")
        self.batch = Batch.objects.create(
            product=self.product, batch_no="B-1", quantity=2500,
            cost_price=Decimal("20.00"),
            expiry_date=timezone.localdate() + timedelta(days=365),
        )

        self.monday = monday_of(timezone.localdate())
        self.plan = WeeklyPlan.objects.create(
            employee=self.mr, week_start=self.monday, status="approved",
        )
        self.visit = PlanVisit.objects.create(
            plan=self.plan, call_point=self.doctor, day=0,
            objective="Detail Panadol",
        )

    def report(self, **overrides):
        payload = {
            "employee": self.mr.pk,
            "call_point": self.doctor.pk,
            "visit_date": self.monday.isoformat(),
            "visit_time": "",
            "doctor_name": "Dr. Ahmed Khan",
            "speciality": "Cardiology",
            "outcome": CallReport.MET,
            "products": [self.product.pk],
            "feedback": "Agreed to trial",
            "next_visit_date": "",
            "new_call_point": "",
            "new_call_point_kind": "doctor",
            "new_call_point_territory": "",
        }
        payload.update(overrides)

        return self.client.post(reverse("call_report_new"), payload)

    # ---------------------------------------------------------- recording

    def test_a_visit_records_the_doctor_seen(self):
        self.report()

        report = CallReport.objects.get()
        self.assertEqual(report.employee, self.mr)
        self.assertEqual(report.call_point, self.doctor)
        self.assertEqual(report.doctor_name, "Dr. Ahmed Khan")
        self.assertEqual(report.speciality, "Cardiology")
        self.assertEqual(report.outcome, CallReport.MET)
        self.assertEqual(list(report.products.all()), [self.product])
        self.assertEqual(report.created_by, self.user)

    def test_reporting_against_a_scheduled_slot_closes_it(self):
        self.client.post(
            reverse("call_report_for_visit", args=[self.visit.pk]),
            {
                "employee": self.mr.pk, "call_point": self.doctor.pk,
                "visit_date": self.monday.isoformat(), "visit_time": "",
                "doctor_name": "Dr. Ahmed Khan", "speciality": "",
                "outcome": CallReport.MET, "feedback": "Good meeting",
                "next_visit_date": "", "new_call_point": "",
                "new_call_point_kind": "doctor", "new_call_point_territory": "",
            },
        )

        self.visit.refresh_from_db()
        self.assertEqual(self.visit.status, "done")
        self.assertEqual(self.visit.remarks, "Good meeting")

        report = CallReport.objects.get()
        self.assertEqual(report.plan_visit, self.visit)
        self.assertTrue(report.was_planned)

    def test_a_doctor_who_was_not_in_marks_the_slot_missed(self):
        self.client.post(
            reverse("call_report_for_visit", args=[self.visit.pk]),
            {
                "employee": self.mr.pk, "call_point": self.doctor.pk,
                "visit_date": self.monday.isoformat(), "visit_time": "",
                "doctor_name": "", "speciality": "",
                "outcome": CallReport.NOT_AVAILABLE, "feedback": "",
                "next_visit_date": "", "new_call_point": "",
                "new_call_point_kind": "doctor", "new_call_point_territory": "",
            },
        )

        self.visit.refresh_from_db()
        self.assertEqual(self.visit.status, "missed")

    def test_a_visit_off_the_plan_is_unplanned(self):
        self.report()

        self.assertFalse(CallReport.objects.get().was_planned)

    # ----------------------------------------------------- new call points

    def test_an_unlisted_doctor_can_be_added_from_the_form(self):
        self.report(call_point="", new_call_point="Dr. Sana Malik")

        created = CallPoint.objects.get(name="Dr. Sana Malik")
        self.assertEqual(created.territory, self.territory)
        self.assertEqual(created.kind, "doctor")
        self.assertEqual(CallReport.objects.get().call_point, created)

    def test_the_same_new_name_twice_makes_one_call_point(self):
        self.report(call_point="", new_call_point="Dr. Sana Malik")
        self.report(call_point="", new_call_point="Dr. Sana Malik")

        self.assertEqual(CallPoint.objects.filter(name="Dr. Sana Malik").count(), 1)
        self.assertEqual(CallReport.objects.count(), 2)

    def test_a_report_needs_a_call_point_one_way_or_the_other(self):
        response = self.report(call_point="", new_call_point="")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(CallReport.objects.count(), 0)
        self.assertContains(response, "Pick a call point")

    def test_a_new_call_point_needs_a_territory_to_fall_back_on(self):
        self.mr.territory = None
        self.mr.save()

        response = self.report(call_point="", new_call_point="Dr. Nobody")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(CallPoint.objects.filter(name="Dr. Nobody").count(), 0)
        self.assertEqual(CallReport.objects.count(), 0)

    # ----------------------------------------------------------- sampling

    def test_samples_left_on_a_call_come_out_of_stock(self):
        self.report(**{"batch[]": [str(self.batch.pk)], "qty[]": ["12"]})

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity, 2488)

        report = CallReport.objects.get()
        self.assertIsNotNone(report.sample_issue)
        self.assertEqual(report.samples_given, 12)
        self.assertEqual(report.sample_issue.call_point, self.doctor)
        self.assertEqual(report.sample_issue.employee, self.mr)

        movement = StockMovement.objects.get(kind=StockMovement.SAMPLE)
        self.assertEqual(movement.quantity, -12)

    def test_a_call_with_no_samples_leaves_stock_alone(self):
        self.report()

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity, 2500)
        self.assertIsNone(CallReport.objects.get().sample_issue)
        self.assertEqual(CallReport.objects.get().samples_given, 0)

    def test_oversampling_saves_nothing_at_all(self):
        response = self.report(**{"batch[]": [str(self.batch.pk)], "qty[]": ["9000"]})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(CallReport.objects.count(), 0)
        self.assertEqual(SampleIssue.objects.count(), 0)

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity, 2500)

    # -------------------------------------------------------------- my day

    def test_my_day_defaults_to_the_signed_in_member(self):
        response = self.client.get(reverse("daily_calls"))

        self.assertEqual(response.context["employee"], self.mr)

    def test_my_day_shows_the_schedule_and_what_was_reported(self):
        self.report(**{"visit_date": self.monday.isoformat()})

        response = self.client.get(
            reverse("daily_calls"), {"date": self.monday.isoformat()}
        )

        self.assertEqual(len(response.context["scheduled"]), 1)
        self.assertEqual(len(response.context["reported"]), 1)
        self.assertEqual(len(response.context["unplanned"]), 1)

    def test_a_reported_slot_is_flagged_done_on_my_day(self):
        self.client.post(
            reverse("call_report_for_visit", args=[self.visit.pk]),
            {
                "employee": self.mr.pk, "call_point": self.doctor.pk,
                "visit_date": self.monday.isoformat(), "visit_time": "",
                "doctor_name": "", "speciality": "", "outcome": CallReport.MET,
                "feedback": "", "next_visit_date": "", "new_call_point": "",
                "new_call_point_kind": "doctor", "new_call_point_territory": "",
            },
        )

        response = self.client.get(
            reverse("daily_calls"), {"date": self.monday.isoformat()}
        )

        self.assertTrue(response.context["scheduled"][0]["done"])
        self.assertEqual(response.context["unplanned"], [])

    def test_my_day_counts_samples_left_that_day(self):
        self.report(**{"batch[]": [str(self.batch.pk)], "qty[]": ["7"]})

        response = self.client.get(
            reverse("daily_calls"), {"date": self.monday.isoformat()}
        )

        self.assertEqual(response.context["samples_today"], 7)

    def test_my_day_only_shows_the_day_asked_for(self):
        self.report(visit_date=(self.monday + timedelta(days=1)).isoformat())

        response = self.client.get(
            reverse("daily_calls"), {"date": self.monday.isoformat()}
        )

        self.assertEqual(response.context["reported"], [])

    # ------------------------------------------------------------ reports

    def test_the_visit_list_filters_by_member_and_month(self):
        other = Employee.objects.create(
            employee_code="MR-02", full_name="Bilal Khan",
            designation="mr", territory=self.territory,
        )
        self.report()
        self.report(employee=other.pk)

        response = self.client.get(
            reverse("call_report_list"), {"employee": self.mr.pk}
        )

        self.assertEqual(len(response.context["reports"]), 1)
        self.assertEqual(response.context["reports"][0].employee, self.mr)

        response = self.client.get(
            reverse("call_report_list"), {"month": self.monday.strftime("%Y-%m")}
        )

        self.assertEqual(len(response.context["reports"]), 2)

    def test_coverage_ranks_members_by_calls_made(self):
        other = Employee.objects.create(
            employee_code="MR-02", full_name="Bilal Khan",
            designation="mr", territory=self.territory,
        )
        self.report()
        self.report()
        self.report(employee=other.pk, outcome=CallReport.NOT_AVAILABLE)

        rows = self.client.get(reverse("call_report_summary")).context["rows"]

        self.assertEqual([row["name"] for row in rows], ["Ali Raza", "Bilal Khan"])
        self.assertEqual(rows[0]["calls"], 2)
        self.assertEqual(rows[0]["met"], 2)
        self.assertEqual(rows[0]["met_percent"], 100)
        self.assertEqual(rows[0]["doctors"], 1)
        self.assertEqual(rows[1]["met"], 0)
        self.assertEqual(rows[1]["met_percent"], 0)

    def test_call_pages_render(self):
        self.report(**{"batch[]": [str(self.batch.pk)], "qty[]": ["3"]})

        for url in (reverse("daily_calls"),
                    reverse("call_report_new"),
                    reverse("call_report_for_visit", args=[self.visit.pk]),
                    reverse("call_report_list"),
                    reverse("call_report_summary")):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)


class InvoiceReprintTests(TestCase):
    """A reprint is a copy of the original document, not a fresh invoice."""

    def setUp(self):
        self.user = User.objects.create_user("clerk", password="pw")
        self.client.login(username="clerk", password="pw")

        self.product = Product.objects.create(code="PAN500", name="Panadol")
        self.batch = Batch.objects.create(
            product=self.product, batch_no="B-1", quantity=100,
            cost_price=Decimal("20.00"),
            expiry_date=timezone.localdate() + timedelta(days=365),
        )

    def generate(self, stock=False, qty="2"):
        return self.client.post(reverse("generate"), {
            "customer_name": "Shifa Pharmacy", "address": "Lahore",
            "ntn": "1", "sales_tax": "2", "license_no": "L-9",
            "item_name[]": ["Panadol"], "qty[]": [qty], "price[]": ["100"],
            "discount[]": ["10"], "batch[]": ["B-1"], "expiry[]": ["12/26"],
            "stock_batch[]": [str(self.batch.pk) if stock else ""],
        })

    def reprint(self, invoice):
        return self.client.get(reverse("invoice_reprint", args=[invoice.pk]))

    def text_of(self, response):
        import pymupdf

        document = pymupdf.open(stream=response.content, filetype="pdf")

        return "\n".join(page.get_text() for page in document)

    # -------------------------------------------------------- the document

    def test_a_reprint_matches_the_original_download(self):
        original = self.text_of(self.generate())
        invoice = Invoice.objects.get()

        copy = self.text_of(self.reprint(invoice))

        self.assertEqual(copy, original)

    def test_the_reprint_is_a_pdf_named_after_the_invoice(self):
        self.generate()
        invoice = Invoice.objects.get()

        response = self.reprint(invoice)

        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn(f'filename="{invoice.invoice_no}.pdf"',
                      response["Content-Disposition"])

    def test_the_reprint_carries_the_original_date_not_today(self):
        self.generate()

        invoice = Invoice.objects.get()
        Invoice.objects.filter(pk=invoice.pk).update(date=date(2026, 1, 15))
        invoice.refresh_from_db()

        self.assertIn("15/01/2026", self.text_of(self.reprint(invoice)))

    def test_the_lines_come_back_verbatim(self):
        self.generate()
        invoice = Invoice.objects.get()

        text = self.text_of(self.reprint(invoice))

        self.assertIn("Panadol", text)
        self.assertIn("B-1", text)
        self.assertIn("12/26", text)
        self.assertIn("180.00", text)      # 2 x 100 less 10%

    # ------------------------------------------------- changes nothing else

    def test_reprinting_does_not_move_stock(self):
        self.generate(stock=True)
        invoice = Invoice.objects.get()

        self.batch.refresh_from_db()
        after_sale = self.batch.quantity

        self.reprint(invoice)
        self.reprint(invoice)

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity, after_sale)
        self.assertEqual(
            StockMovement.objects.filter(kind=StockMovement.SALE).count(), 1
        )

    def test_reprinting_creates_no_second_invoice(self):
        self.generate()
        invoice = Invoice.objects.get()
        number = invoice.invoice_no

        self.reprint(invoice)

        self.assertEqual(Invoice.objects.count(), 1)
        self.assertEqual(Item.objects.count(), 1)
        self.assertEqual(Invoice.objects.get().invoice_no, number)

    def test_reprinting_leaves_the_ledger_alone(self):
        self.generate()
        invoice = Invoice.objects.get()
        owed = invoice.customer.outstanding_balance

        self.reprint(invoice)

        invoice.customer.refresh_from_db()
        self.assertEqual(invoice.customer.outstanding_balance, owed)

    def test_every_reprint_is_logged(self):
        self.generate()
        invoice = Invoice.objects.get()

        self.reprint(invoice)
        self.reprint(invoice)

        logs = InvoiceLog.objects.filter(action="Invoice Reprinted")
        self.assertEqual(logs.count(), 2)
        self.assertEqual(logs.first().user, self.user)
        self.assertEqual(logs.first().amount, invoice.total)

    # ------------------------------------------------- the previous balance

    def test_the_copy_shows_the_balance_the_customer_saw_then(self):
        """A payment made after the invoice must not change its reprint."""
        self.generate()                     # HHC-9965, 180.00
        self.generate()                     # HHC-9966, carries 180.00

        first, second = Invoice.objects.order_by("id")
        Invoice.objects.filter(pk=first.pk).update(date=date(2026, 1, 1))
        Invoice.objects.filter(pk=second.pk).update(date=date(2026, 1, 10))

        Payment.objects.create(
            customer=first.customer, invoice=first,
            amount=Decimal("180.00"), paid_on=date(2026, 2, 1),
        )

        text = self.text_of(self.reprint(Invoice.objects.get(pk=second.pk)))

        self.assertIn("PREVIOUS OUTSTANDING", text)
        self.assertIn(first.invoice_no, text)
        self.assertIn("360.00", text)       # 180 carried + 180 this invoice

    def test_a_payment_made_before_the_invoice_still_counts(self):
        self.generate()
        self.generate()

        first, second = Invoice.objects.order_by("id")
        Invoice.objects.filter(pk=first.pk).update(date=date(2026, 1, 1))
        Invoice.objects.filter(pk=second.pk).update(date=date(2026, 1, 10))

        Payment.objects.create(
            customer=first.customer, invoice=first,
            amount=Decimal("100.00"), paid_on=date(2026, 1, 5),
        )

        text = self.text_of(self.reprint(Invoice.objects.get(pk=second.pk)))

        self.assertIn("80.00", text)        # 180 - 100 outstanding at the time

    def test_later_invoices_stay_off_an_earlier_reprint(self):
        self.generate()
        self.generate()

        first, second = Invoice.objects.order_by("id")
        Invoice.objects.filter(pk=first.pk).update(date=date(2026, 1, 1))
        Invoice.objects.filter(pk=second.pk).update(date=date(2026, 1, 10))

        text = self.text_of(self.reprint(Invoice.objects.get(pk=first.pk)))

        self.assertNotIn("PREVIOUS OUTSTANDING", text)
        self.assertNotIn(second.invoice_no, text)

    def test_goods_returned_later_stay_on_the_reprint(self):
        """A credit note is its own paperwork; the invoice was still issued."""
        self.generate(stock=True)
        invoice = Invoice.objects.get()

        item = invoice.items.first()

        self.client.post(reverse("return_create", args=[invoice.pk]), {
            "reason": "Damaged", "restock": "on",
            "date": timezone.localdate().isoformat(),
            f"qty_{item.pk}": "2",
        })

        self.assertEqual(SalesReturn.objects.count(), 1)

        text = self.text_of(self.reprint(invoice))

        self.assertIn("Panadol", text)
        self.assertIn("180.00", text)

    # ------------------------------------------------------------- the list

    def test_the_invoice_list_finds_an_invoice_by_number(self):
        self.generate()
        invoice = Invoice.objects.get()

        response = self.client.get(
            reverse("invoice_list"), {"q": invoice.invoice_no}
        )

        self.assertEqual(list(response.context["invoices"]), [invoice])

    def test_the_invoice_list_finds_an_invoice_by_customer(self):
        self.generate()

        response = self.client.get(reverse("invoice_list"), {"q": "shifa"})

        self.assertEqual(len(response.context["invoices"]), 1)

    def test_the_invoice_list_filters_by_settlement(self):
        self.generate()
        self.generate()

        first = Invoice.objects.order_by("id").first()
        Payment.objects.create(
            customer=first.customer, invoice=first, amount=first.total
        )

        unpaid = self.client.get(reverse("invoice_list"), {"status": "unpaid"})
        paid = self.client.get(reverse("invoice_list"), {"status": "paid"})

        self.assertEqual(len(unpaid.context["invoices"]), 1)
        self.assertEqual(list(paid.context["invoices"]), [first])

    def test_the_invoice_list_totals_what_it_shows(self):
        self.generate()
        self.generate()

        response = self.client.get(reverse("invoice_list"))

        self.assertEqual(response.context["total"], Decimal("360.00"))
        self.assertEqual(response.context["outstanding"], Decimal("360.00"))

    def test_the_invoice_list_renders(self):
        self.generate()

        self.assertEqual(self.client.get(reverse("invoice_list")).status_code, 200)


class CommissionTests(TestCase):
    """MRs on salary plus a percentage of what they actually sell."""

    def setUp(self):
        self.user = User.objects.create_user("clerk", password="pw")
        self.client.login(username="clerk", password="pw")

        self.territory = Territory.objects.create(name="Gulberg", city="Lahore")

        self.mr = Employee.objects.create(
            employee_code="MR-01", full_name="Ali Raza", designation="mr",
            territory=self.territory, basic_salary=Decimal("60000"),
            commission_percent=Decimal("10.00"),
        )
        self.salaried = Employee.objects.create(
            employee_code="OF-01", full_name="Bilal Khan", designation="admin",
            basic_salary=Decimal("40000"),
        )

        self.product = Product.objects.create(code="PAN500", name="Panadol")
        self.batch = Batch.objects.create(
            product=self.product, batch_no="B-1", quantity=1000,
            cost_price=Decimal("20.00"),
            expiry_date=timezone.localdate() + timedelta(days=365),
        )

    def generate(self, rep=None, price="1000", qty="1", discount="0",
                 customer="Shifa Pharmacy", stock=False):
        return self.client.post(reverse("generate"), {
            "customer_name": customer, "address": "Lahore",
            "ntn": "1", "sales_tax": "2", "license_no": "L",
            "sales_rep": str(rep.pk) if rep else "",
            "item_name[]": ["Panadol"], "qty[]": [qty], "price[]": [price],
            "discount[]": [discount], "batch[]": ["B-1"], "expiry[]": ["12/26"],
            "stock_batch[]": [str(self.batch.pk) if stock else ""],
        })

    def run_payroll(self, month=None):
        return self.client.post(reverse("payroll_create"), {
            "month": (month or timezone.localdate()).isoformat(), "note": "",
        })

    # ------------------------------------------------ crediting the sale

    def test_an_invoice_is_credited_to_the_chosen_mr(self):
        self.generate(rep=self.mr)

        self.assertEqual(Invoice.objects.get().sales_rep, self.mr)

    def test_the_territory_mr_is_credited_when_none_is_picked(self):
        customer = Customer.objects.create(
            name="Shifa Pharmacy", territory=self.territory
        )

        self.generate()

        self.assertEqual(Invoice.objects.get().sales_rep, self.mr)
        self.assertEqual(Invoice.objects.get().customer, customer)

    def test_nobody_is_credited_when_two_mrs_share_a_territory(self):
        """A guess here would pay the wrong person, so it credits no one."""
        Employee.objects.create(
            employee_code="MR-02", full_name="Sara", designation="mr",
            territory=self.territory,
        )
        Customer.objects.create(name="Shifa Pharmacy", territory=self.territory)

        self.generate()

        self.assertIsNone(Invoice.objects.get().sales_rep)

    def test_a_customer_with_no_territory_credits_nobody(self):
        self.generate()

        self.assertIsNone(Invoice.objects.get().sales_rep)

    # --------------------------------------------------- what counts as sales

    def test_commission_is_worked_out_after_discounts(self):
        self.generate(rep=self.mr, price="1000", qty="10", discount="20")

        start, end = month_range(timezone.localdate())

        # 10 x 1000 less 20% = 8,000 net; 10% of that is 800.
        self.assertEqual(self.mr.net_sales(start, end), Decimal("8000.00"))
        self.assertEqual(self.mr.commission_on(start, end), Decimal("800.00"))

    def test_returned_goods_come_off_the_commission(self):
        self.generate(rep=self.mr, price="1000", qty="10", stock=True)

        invoice = Invoice.objects.get()

        item = invoice.items.first()

        self.client.post(reverse("return_create", args=[invoice.pk]), {
            "reason": "Damaged", "restock": "on",
            "date": timezone.localdate().isoformat(),
            f"qty_{item.pk}": "4",
        })

        self.assertEqual(SalesReturn.objects.count(), 1)

        start, end = month_range(timezone.localdate())

        # 10,000 invoiced less 4,000 credited = 6,000 net; 10% is 600.
        self.assertEqual(self.mr.net_sales(start, end), Decimal("6000.00"))
        self.assertEqual(self.mr.commission_on(start, end), Decimal("600.00"))

    def test_another_mrs_sales_are_not_counted(self):
        other = Employee.objects.create(
            employee_code="MR-02", full_name="Sara", designation="mr",
        )
        self.generate(rep=other, price="5000")

        start, end = month_range(timezone.localdate())

        self.assertEqual(self.mr.net_sales(start, end), ZERO)

    def test_sales_outside_the_month_are_not_counted(self):
        self.generate(rep=self.mr, price="1000")

        Invoice.objects.update(date=date(2026, 1, 15))

        start, end = month_range(date(2026, 2, 1))

        self.assertEqual(self.mr.net_sales(start, end), ZERO)
        self.assertEqual(
            self.mr.net_sales(*month_range(date(2026, 1, 1))), Decimal("1000.00")
        )

    # ------------------------------------------------------------- payroll

    def test_payroll_pays_commission_on_top_of_salary(self):
        self.generate(rep=self.mr, price="1000", qty="10", discount="20")

        self.run_payroll()

        slip = Payslip.objects.get(employee=self.mr)

        self.assertEqual(slip.sales_amount, Decimal("8000.00"))
        self.assertEqual(slip.commission_percent, Decimal("10.00"))
        self.assertEqual(slip.commission, Decimal("800.00"))
        self.assertEqual(slip.gross_pay, Decimal("60800.00"))
        self.assertEqual(slip.net_pay, Decimal("60800.00"))

    def test_salary_only_staff_get_no_commission_lines(self):
        self.generate(rep=self.salaried, price="9000")

        self.run_payroll()

        slip = Payslip.objects.get(employee=self.salaried)

        self.assertEqual(slip.sales_amount, ZERO)
        self.assertEqual(slip.commission, ZERO)
        self.assertEqual(slip.gross_pay, Decimal("40000.00"))

    def test_the_rate_is_frozen_onto_the_slip(self):
        """A later change of rate must not rewrite a slip already issued."""
        self.generate(rep=self.mr, price="1000", qty="10")

        self.run_payroll()

        self.mr.commission_percent = Decimal("20.00")
        self.mr.save()

        slip = Payslip.objects.get(employee=self.mr)

        self.assertEqual(slip.commission_percent, Decimal("10.00"))
        self.assertEqual(slip.commission, Decimal("1000.00"))

    def test_editing_a_slip_recomputes_the_commission(self):
        self.run_payroll()

        slip = Payslip.objects.get(employee=self.mr)

        self.client.post(reverse("payslip_edit", args=[slip.pk]), {
            "basic_salary": "60000", "fuel_allowance": "0",
            "mobile_allowance": "0", "other_allowance": "0",
            "expense_reimbursement": "0", "sales_amount": "50000",
            "commission_percent": "15", "tax_deduction": "0",
            "advance_deduction": "0", "other_deduction": "0", "note": "",
        })

        slip.refresh_from_db()

        self.assertEqual(slip.commission, Decimal("7500.00"))
        self.assertEqual(slip.gross_pay, Decimal("67500.00"))

    def test_the_payslip_pdf_shows_the_rate_and_the_sales_behind_it(self):
        import pymupdf

        self.generate(rep=self.mr, price="1000", qty="10", discount="20")
        self.run_payroll()

        slip = Payslip.objects.get(employee=self.mr)
        response = self.client.get(reverse("payslip_pdf", args=[slip.pk]))

        text = pymupdf.open(stream=response.content, filetype="pdf")[0].get_text()

        self.assertIn("Sales Commission @ 10%", text)
        self.assertIn("8,000.00", text)
        self.assertIn("800.00", text)

    # ------------------------------------------------------------ reporting

    def test_the_commission_report_ranks_earners(self):
        self.generate(rep=self.mr, price="1000", qty="10")

        response = self.client.get(reverse("commission_report"))

        row = response.context["rows"][0]

        self.assertEqual(row["employee"], self.mr)
        self.assertEqual(row["sales"], Decimal("10000.00"))
        self.assertEqual(row["commission"], Decimal("1000.00"))
        self.assertEqual(response.context["total_commission"], Decimal("1000.00"))

    def test_the_report_flags_invoices_credited_to_nobody(self):
        self.generate(price="4000")

        response = self.client.get(reverse("commission_report"))

        self.assertEqual(response.context["unattributed"], 1)
        self.assertEqual(response.context["unattributed_value"], Decimal("4000.00"))

    def test_commission_pages_render(self):
        self.generate(rep=self.mr)
        self.run_payroll()

        for url in (reverse("commission_report"), reverse("payroll_list")):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)


class FieldLoginTests(TestCase):
    """An MR signs in and sees their own work — and only their own."""

    def setUp(self):
        self.territory = Territory.objects.create(name="Gulberg", city="Lahore")
        self.other_territory = Territory.objects.create(name="Model Town")

        self.user = User.objects.create_user("ali", password="pw12345678")
        UserRolls.objects.filter(user=self.user).update(
            role=UserRolls.ROLE_FIELD
        )

        self.mr = Employee.objects.create(
            employee_code="MR-01", full_name="Ali Raza", designation="mr",
            territory=self.territory, user=self.user,
            basic_salary=Decimal("50000"), commission_percent=Decimal("10.00"),
        )
        self.other = Employee.objects.create(
            employee_code="MR-02", full_name="Sara Khan", designation="mr",
            territory=self.other_territory,
        )

        self.doctor = CallPoint.objects.create(
            name="Dr. Ahmed", territory=self.territory, kind="doctor"
        )
        self.other_doctor = CallPoint.objects.create(
            name="Dr. Zubair", territory=self.other_territory, kind="doctor"
        )

        self.client.login(username="ali", password="pw12345678")

    # ------------------------------------------------------------- the fence

    def test_signing_in_lands_on_the_portal(self):
        self.client.logout()

        response = self.client.post(reverse("login"), {
            "username": "ali", "password": "pw12345678",
        })

        self.assertRedirects(response, reverse("my_dashboard"))

    def test_office_pages_are_closed_to_field_staff(self):
        for name in ("index", "dashboard", "customer_list", "ledger_list",
                     "purchase_list", "stock_report", "payroll_list",
                     "team_list", "invoice_list", "commission_report",
                     "distributor_list", "expense_report", "invoice_logs",
                     "call_report_summary", "territory_report"):
            with self.subTest(page=name):
                response = self.client.get(reverse(name))

                self.assertRedirects(response, reverse("my_dashboard"))

    def test_the_portal_is_open(self):
        for name in ("my_dashboard", "my_plan", "my_sales", "my_payslips",
                     "daily_calls", "call_report_list", "call_report_new",
                     "call_point_list", "sample_list", "sample_new",
                     "expense_list", "profile"):
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_the_office_is_not_fenced_in(self):
        self.client.logout()

        User.objects.create_user("clerk", password="pw")
        self.client.login(username="clerk", password="pw")

        self.assertEqual(self.client.get(reverse("purchase_list")).status_code, 200)
        self.assertEqual(self.client.get(reverse("payroll_list")).status_code, 200)

    def test_a_superuser_is_never_treated_as_field_staff(self):
        """A mis-set role must not lock an admin out of their own system."""
        admin = User.objects.create_superuser("boss", password="pw")
        UserRolls.objects.filter(user=admin).update(role=UserRolls.ROLE_FIELD)

        self.client.logout()
        self.client.login(username="boss", password="pw")

        self.assertEqual(self.client.get(reverse("team_list")).status_code, 200)

    # -------------------------------------------------------- what they see

    def test_only_their_own_call_points(self):
        response = self.client.get(reverse("call_point_list"))

        names = [c.name for c in response.context["call_points"]]

        self.assertIn("Dr. Ahmed", names)
        self.assertNotIn("Dr. Zubair", names)

    def test_only_their_own_plans(self):
        WeeklyPlan.objects.create(
            employee=self.mr, week_start=monday_of(timezone.localdate())
        )
        WeeklyPlan.objects.create(
            employee=self.other, week_start=monday_of(timezone.localdate())
        )

        response = self.client.get(reverse("plan_list"))

        self.assertEqual(
            [p.employee for p in response.context["plans"]], [self.mr]
        )

    def test_only_their_own_visits(self):
        CallReport.objects.create(employee=self.mr, call_point=self.doctor)
        CallReport.objects.create(
            employee=self.other, call_point=self.other_doctor
        )

        response = self.client.get(reverse("call_report_list"))

        self.assertEqual(len(response.context["reports"]), 1)
        self.assertEqual(response.context["reports"][0].employee, self.mr)

    def test_my_day_cannot_be_pointed_at_someone_else(self):
        response = self.client.get(
            reverse("daily_calls"), {"employee": self.other.pk}
        )

        self.assertEqual(response.context["employee"], self.mr)

    def test_only_their_own_expenses(self):
        category = ExpenseCategory.objects.create(name="Fuel")

        Expense.objects.create(
            category=category, employee=self.mr, amount=Decimal("500"),
            date=timezone.localdate(),
        )
        Expense.objects.create(
            category=category, employee=self.other, amount=Decimal("900"),
            date=timezone.localdate(),
        )

        response = self.client.get(reverse("expense_list"))

        self.assertEqual(len(response.context["expenses"]), 1)
        self.assertEqual(response.context["expenses"][0].employee, self.mr)

    def test_only_their_own_samples(self):
        SampleIssue.objects.create(employee=self.mr, call_point=self.doctor)
        SampleIssue.objects.create(
            employee=self.other, call_point=self.other_doctor
        )

        response = self.client.get(reverse("sample_list"))

        self.assertEqual(len(response.context["issues"]), 1)
        self.assertEqual(response.context["issues"][0].employee, self.mr)

    # --------------------------------------------- records, not just pages

    def test_another_members_plan_is_refused_by_id(self):
        plan = WeeklyPlan.objects.create(
            employee=self.other, week_start=monday_of(timezone.localdate())
        )

        response = self.client.get(reverse("plan_detail", args=[plan.pk]))

        self.assertRedirects(response, reverse("my_dashboard"))

    def test_another_members_payslip_cannot_be_downloaded(self):
        run = PayrollRun.objects.create(month=timezone.localdate())
        slip = Payslip.objects.create(run=run, employee=self.other)

        response = self.client.get(reverse("payslip_pdf", args=[slip.pk]))

        self.assertRedirects(response, reverse("my_dashboard"))

    def test_their_own_payslip_downloads(self):
        run = PayrollRun.objects.create(month=timezone.localdate())
        slip = Payslip.objects.create(run=run, employee=self.mr)

        response = self.client.get(reverse("payslip_pdf", args=[slip.pk]))

        self.assertEqual(response["Content-Type"], "application/pdf")

    def test_another_members_expense_cannot_be_edited(self):
        category = ExpenseCategory.objects.create(name="Fuel")
        expense = Expense.objects.create(
            category=category, employee=self.other, amount=Decimal("900"),
            date=timezone.localdate(),
        )

        response = self.client.get(reverse("expense_edit", args=[expense.pk]))

        self.assertRedirects(response, reverse("my_dashboard"))

    def test_a_scheduled_call_of_anothers_cannot_be_reported(self):
        plan = WeeklyPlan.objects.create(
            employee=self.other, week_start=monday_of(timezone.localdate())
        )
        visit = PlanVisit.objects.create(
            plan=plan, call_point=self.other_doctor, day=0
        )

        response = self.client.get(
            reverse("call_report_for_visit", args=[visit.pk])
        )

        self.assertRedirects(response, reverse("my_dashboard"))

    def test_a_visit_filed_under_another_name_is_reassigned(self):
        """The form can say anything; the record files under the login."""
        self.client.post(reverse("call_report_new"), {
            "employee": self.other.pk, "call_point": self.doctor.pk,
            "visit_date": timezone.localdate().isoformat(), "visit_time": "",
            "doctor_name": "Dr. Ahmed", "speciality": "",
            "outcome": CallReport.MET, "feedback": "", "next_visit_date": "",
            "new_call_point": "", "new_call_point_kind": "doctor",
            "new_call_point_territory": "",
        })

        self.assertEqual(CallReport.objects.get().employee, self.mr)

    def test_an_expense_claimed_for_another_is_reassigned_and_pending(self):
        category = ExpenseCategory.objects.create(name="Fuel")

        self.client.post(reverse("expense_new"), {
            "category": category.pk, "employee": self.other.pk,
            "amount": "750", "date": timezone.localdate().isoformat(),
            "description": "Petrol", "status": Expense.APPROVED,
        })

        expense = Expense.objects.get()

        self.assertEqual(expense.employee, self.mr)
        self.assertEqual(expense.status, Expense.PENDING)

    # ------------------------------------------------------ their own money

    def test_my_sales_shows_only_their_invoices(self):
        mine = Invoice.objects.create(
            customer=Customer.objects.create(name="Shifa"),
            sales_rep=self.mr, total=Decimal("5000.00"), license_no="L",
        )
        Invoice.objects.create(
            customer=Customer.objects.create(name="Other"),
            sales_rep=self.other, total=Decimal("9000.00"), license_no="L",
        )

        response = self.client.get(reverse("my_sales"))

        self.assertEqual(list(response.context["invoices"]), [mine])
        self.assertEqual(response.context["net"], Decimal("5000.00"))
        self.assertEqual(response.context["commission"], Decimal("500.00"))

    def test_my_payslips_shows_only_their_own(self):
        run = PayrollRun.objects.create(month=timezone.localdate())
        mine = Payslip.objects.create(run=run, employee=self.mr)
        Payslip.objects.create(run=run, employee=self.other)

        response = self.client.get(reverse("my_payslips"))

        self.assertEqual(list(response.context["payslips"]), [mine])

    def test_the_dashboard_shows_this_month_at_a_glance(self):
        Invoice.objects.create(
            customer=Customer.objects.create(name="Shifa"),
            sales_rep=self.mr, total=Decimal("20000.00"), license_no="L",
        )
        CallReport.objects.create(employee=self.mr, call_point=self.doctor)

        response = self.client.get(reverse("my_dashboard"))

        self.assertEqual(response.context["me"], self.mr)
        self.assertEqual(response.context["sales"], Decimal("20000.00"))
        self.assertEqual(response.context["commission"], Decimal("2000.00"))
        self.assertEqual(response.context["month_calls"], 1)

    def test_a_login_with_no_team_member_sees_nothing_rather_than_everything(self):
        self.mr.user = None
        self.mr.save()

        response = self.client.get(reverse("my_dashboard"))

        self.assertIsNone(response.context["me"])
        self.assertEqual(
            self.client.get(reverse("my_sales")).context["invoices"], []
        )


class EmployeeLoginSetupTests(TestCase):
    """The office hands an MR their credentials."""

    def setUp(self):
        self.user = User.objects.create_user("clerk", password="pw")
        self.client.login(username="clerk", password="pw")

        self.employee = Employee.objects.create(
            employee_code="MR-01", full_name="Ali Raza", designation="mr",
            phone="0300-1234567", email="ali@example.com",
        )

    def create_login(self, **overrides):
        payload = {"username": "ali", "password": "pw12345678"}
        payload.update(overrides)

        return self.client.post(
            reverse("employee_login", args=[self.employee.pk]), payload
        )

    def test_a_login_is_created_linked_and_marked_field_staff(self):
        self.create_login()

        self.employee.refresh_from_db()

        self.assertIsNotNone(self.employee.user)
        self.assertEqual(self.employee.user.username, "ali")
        self.assertTrue(self.employee.user.check_password("pw12345678"))
        self.assertEqual(
            self.employee.user.userrolls.role, UserRolls.ROLE_FIELD
        )

    def test_the_new_login_can_sign_in_and_reaches_its_portal(self):
        self.create_login()
        self.client.logout()

        response = self.client.post(reverse("login"), {
            "username": "ali", "password": "pw12345678",
        })

        self.assertRedirects(response, reverse("my_dashboard"))

    def test_a_taken_username_is_refused(self):
        User.objects.create_user("ali", password="something")

        self.create_login()

        self.employee.refresh_from_db()
        self.assertIsNone(self.employee.user)

    def test_a_short_password_is_refused(self):
        self.create_login(password="short")

        self.employee.refresh_from_db()
        self.assertIsNone(self.employee.user)

    def test_resetting_keeps_the_same_account(self):
        self.create_login()

        account_id = Employee.objects.get(pk=self.employee.pk).user_id

        self.create_login(username="ignored", password="newpass12345")

        self.employee.refresh_from_db()

        self.assertEqual(self.employee.user_id, account_id)
        self.assertEqual(self.employee.user.username, "ali")
        self.assertTrue(self.employee.user.check_password("newpass12345"))

    def test_field_staff_cannot_hand_out_logins(self):
        self.create_login()
        self.client.logout()
        self.client.login(username="ali", password="pw12345678")

        target = Employee.objects.create(
            employee_code="MR-02", full_name="Sara Khan", designation="mr",
        )

        response = self.client.get(reverse("employee_login", args=[target.pk]))

        self.assertRedirects(response, reverse("my_dashboard"))

    def test_the_setup_page_renders(self):
        self.assertEqual(
            self.client.get(
                reverse("employee_login", args=[self.employee.pk])
            ).status_code,
            200,
        )


class BatchEditTests(TestCase):
    """Expiry belongs to the batch, and a mistyped one has to be correctable."""

    def setUp(self):
        self.user = User.objects.create_user("clerk", password="pw")
        self.client.login(username="clerk", password="pw")

        self.product = Product.objects.create(code="PAN500", name="Panadol")
        self.batch = Batch.objects.create(
            product=self.product, batch_no="B-1", quantity=100,
            received_quantity=100, cost_price=Decimal("20.00"),
            expiry_date=date(2027, 6, 30),
        )

    def edit(self, **overrides):
        payload = {"batch_no": self.batch.batch_no, "expiry_date": "2028-12-31"}
        payload.update(overrides)

        return self.client.post(
            reverse("batch_edit", args=[self.batch.pk]), payload
        )

    def test_the_expiry_date_can_be_corrected(self):
        self.edit(expiry_date="2028-12-31")

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.expiry_date, date(2028, 12, 31))

    def test_the_batch_number_can_be_corrected(self):
        self.edit(batch_no="B-1A")

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.batch_no, "B-1A")

    def test_a_correction_is_written_to_the_stock_ledger(self):
        self.edit(batch_no="B-1A", expiry_date="2028-12-31")

        movement = StockMovement.objects.get(kind=StockMovement.ADJUSTMENT)

        self.assertEqual(movement.quantity, 0)
        self.assertEqual(movement.created_by, self.user)
        self.assertIn("B-1 → B-1A", movement.note)
        self.assertIn("30-06-2027 → 31-12-2028", movement.note)

    def test_saving_without_changing_anything_logs_nothing(self):
        self.edit(expiry_date="2027-06-30")

        self.assertEqual(StockMovement.objects.count(), 0)

    def test_a_correction_moves_no_stock(self):
        self.edit(expiry_date="2028-12-31")

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity, 100)
        self.assertEqual(self.batch.received_quantity, 100)

    def test_two_batches_of_one_product_cannot_share_a_number(self):
        Batch.objects.create(
            product=self.product, batch_no="B-2", quantity=50,
            expiry_date=date(2027, 1, 31),
        )

        response = self.edit(batch_no="B-2")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already has a batch")

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.batch_no, "B-1")

    def test_the_same_number_on_another_product_is_fine(self):
        other = Product.objects.create(code="BRU", name="Brufen")
        Batch.objects.create(
            product=other, batch_no="B-9", quantity=10,
            expiry_date=date(2027, 1, 31),
        )

        self.edit(batch_no="B-9")

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.batch_no, "B-9")

    def test_quantity_cannot_be_typed_over_here(self):
        """Stock only moves through the ledger, never by editing a field."""
        self.client.post(reverse("batch_edit", args=[self.batch.pk]), {
            "batch_no": "B-1", "expiry_date": "2028-12-31", "quantity": "999",
        })

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity, 100)

    def test_correcting_expiry_reorders_fefo(self):
        """The point of the date: it decides what goes out first."""
        later = Batch.objects.create(
            product=self.product, batch_no="B-2", quantity=100,
            expiry_date=date(2028, 1, 31),
        )

        picks, _ = allocate_fefo(self.product, 10)
        self.assertEqual(picks[0][0], self.batch)

        self.edit(expiry_date="2029-12-31")

        picks, _ = allocate_fefo(self.product, 10)
        self.assertEqual(picks[0][0], later)

    def test_an_invoice_keeps_the_batch_number_it_was_printed_with(self):
        customer = Customer.objects.create(name="Shifa")
        invoice = Invoice.objects.create(customer=customer, license_no="L")
        item = Item.objects.create(
            invoice=invoice, name="Panadol", qty=5, batch="B-1",
            expiry="06/27", price=Decimal("100"), discount=ZERO,
            product=self.product, stock_batch=self.batch,
        )

        self.edit(batch_no="B-1A")

        item.refresh_from_db()
        self.assertEqual(item.batch, "B-1")

    def test_the_batch_page_renders(self):
        self.assertEqual(
            self.client.get(reverse("batch_edit", args=[self.batch.pk])).status_code,
            200,
        )


class PurchaseExpiryEditTests(TestCase):
    """Receiving is where an expiry typo happens, so it is fixable there."""

    def setUp(self):
        self.user = User.objects.create_user("clerk", password="pw")
        self.client.login(username="clerk", password="pw")

        self.supplier = Supplier.objects.create(name="Getz Pharma")
        self.product = Product.objects.create(code="PAN500", name="Panadol")

        self.client.post(reverse("purchase_new"), {
            "supplier": self.supplier.pk, "reference": "GRN-1",
            "date": timezone.localdate().isoformat(), "note": "",
            "product[]": [str(self.product.pk)], "batch_no[]": ["B-1"],
            "expiry_date[]": ["2027-06-30"], "quantity[]": ["100"],
            "cost_price[]": ["20.00"],
        })

        self.purchase = Purchase.objects.get()
        self.item = self.purchase.items.get()
        self.batch = self.item.batch

    def post_edit(self, **overrides):
        payload = {
            "supplier": self.supplier.pk, "reference": "GRN-1",
            "date": self.purchase.date.isoformat(), "note": "",
            f"qty_{self.item.pk}": str(self.item.quantity),
            f"cost_{self.item.pk}": str(self.item.cost_price),
            f"expiry_{self.item.pk}": "2027-06-30",
        }
        payload.update(overrides)

        return self.client.post(
            reverse("purchase_edit", args=[self.purchase.pk]), payload
        )

    def test_the_expiry_can_be_corrected_from_the_purchase(self):
        self.post_edit(**{f"expiry_{self.item.pk}": "2029-03-31"})

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.expiry_date, date(2029, 3, 31))

    def test_the_correction_reaches_the_stock_ledger(self):
        self.post_edit(**{f"expiry_{self.item.pk}": "2029-03-31"})

        movement = StockMovement.objects.filter(
            kind=StockMovement.ADJUSTMENT, quantity=0
        ).get()

        self.assertIn("30-06-2027 → 31-03-2029", movement.note)

    def test_correcting_an_expiry_alone_moves_no_stock(self):
        self.post_edit(**{f"expiry_{self.item.pk}": "2029-03-31"})

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity, 100)

    def test_a_blank_expiry_is_refused_and_nothing_is_saved(self):
        response = self.post_edit(**{
            f"expiry_{self.item.pk}": "", f"cost_{self.item.pk}": "25.00",
        })

        self.assertEqual(response.status_code, 200)

        self.batch.refresh_from_db()
        self.item.refresh_from_db()

        self.assertEqual(self.batch.expiry_date, date(2027, 6, 30))
        self.assertEqual(self.item.cost_price, Decimal("20.00"))

    def test_expiry_and_quantity_can_be_corrected_together(self):
        self.post_edit(**{
            f"expiry_{self.item.pk}": "2029-03-31",
            f"qty_{self.item.pk}": "120",
        })

        self.batch.refresh_from_db()

        self.assertEqual(self.batch.expiry_date, date(2029, 3, 31))
        self.assertEqual(self.batch.quantity, 120)


class MyExpensesTests(TestCase):
    """Expense claims belong to one person, and the page should say so."""

    def setUp(self):
        self.category = ExpenseCategory.objects.create(name="Fuel")
        self.territory = Territory.objects.create(name="Gulberg")

        self.clerk = User.objects.create_user("clerk", password="pw")
        self.clerk_employee = Employee.objects.create(
            employee_code="OF-01", full_name="Office Clerk",
            designation="admin", user=self.clerk,
        )
        self.mr = Employee.objects.create(
            employee_code="MR-01", full_name="Ali Raza", designation="mr",
            territory=self.territory,
        )

        Expense.objects.create(
            category=self.category, employee=self.clerk_employee,
            amount=Decimal("300"), date=timezone.localdate(),
        )
        Expense.objects.create(
            category=self.category, employee=self.mr,
            amount=Decimal("900"), date=timezone.localdate(),
        )

        self.client.login(username="clerk", password="pw")

    def test_the_office_sees_the_whole_team_by_default(self):
        response = self.client.get(reverse("expense_list"))

        self.assertEqual(len(response.context["expenses"]), 2)
        self.assertFalse(response.context["only_mine"])

    def test_show_only_mine_narrows_to_the_signed_in_person(self):
        response = self.client.get(reverse("expense_list"), {"mine": "1"})

        self.assertEqual(len(response.context["expenses"]), 1)
        self.assertEqual(
            response.context["expenses"][0].employee, self.clerk_employee
        )
        self.assertEqual(response.context["total"], Decimal("300.00"))

    def test_the_totals_follow_the_narrowed_list(self):
        Expense.objects.create(
            category=self.category, employee=self.clerk_employee,
            amount=Decimal("200"), date=timezone.localdate(),
        )

        response = self.client.get(reverse("expense_list"), {"mine": "1"})

        self.assertEqual(response.context["total"], Decimal("500.00"))
        self.assertEqual(response.context["pending_total"], Decimal("500.00"))

    def test_an_office_login_with_no_team_record_is_offered_nothing(self):
        self.client.logout()
        User.objects.create_user("temp", password="pw")
        self.client.login(username="temp", password="pw")

        response = self.client.get(reverse("expense_list"))

        self.assertIsNone(response.context["me"])
        self.assertEqual(len(response.context["expenses"]), 2)

    def test_a_field_login_is_locked_to_its_own_claims(self):
        account = User.objects.create_user("ali", password="pw12345678")
        UserRolls.objects.filter(user=account).update(
            role=UserRolls.ROLE_FIELD
        )
        self.mr.user = account
        self.mr.save()

        self.client.logout()
        self.client.login(username="ali", password="pw12345678")

        # Even asked for someone else's, it stays on their own.
        response = self.client.get(
            reverse("expense_list"), {"employee": self.clerk_employee.pk}
        )

        self.assertTrue(response.context["locked_to_me"])
        self.assertEqual(len(response.context["expenses"]), 1)
        self.assertEqual(response.context["expenses"][0].employee, self.mr)


class FieldFormScopingTests(TestCase):
    """An MR filing their own visit is not asked who they are."""

    def setUp(self):
        self.territory = Territory.objects.create(name="Gulberg", city="Lahore")
        self.other_territory = Territory.objects.create(name="Model Town")

        self.account = User.objects.create_user("ali", password="pw12345678")
        UserRolls.objects.filter(user=self.account).update(
            role=UserRolls.ROLE_FIELD
        )

        self.mr = Employee.objects.create(
            employee_code="MR-01", full_name="Ali Raza", designation="mr",
            territory=self.territory, user=self.account,
        )
        self.other = Employee.objects.create(
            employee_code="MR-02", full_name="Sara Khan", designation="mr",
            territory=self.other_territory,
        )

        self.mine = CallPoint.objects.create(
            name="Dr. Ahmed", territory=self.territory, kind="doctor"
        )
        self.theirs = CallPoint.objects.create(
            name="Dr. Zubair", territory=self.other_territory, kind="doctor"
        )

        self.client.login(username="ali", password="pw12345678")

    def report(self, **overrides):
        payload = {
            "call_point": self.mine.pk,
            "visit_date": timezone.localdate().isoformat(),
            "visit_time": "", "doctor_name": "Dr. Ahmed", "speciality": "",
            "outcome": CallReport.MET, "feedback": "", "next_visit_date": "",
            "new_call_point": "", "new_call_point_kind": "doctor",
            "new_call_point_territory": "",
        }
        payload.update(overrides)

        return self.client.post(reverse("call_report_new"), payload)

    # ---------------------------------------------------------- the visit form

    def test_the_team_member_field_is_gone_for_a_field_login(self):
        form = self.client.get(reverse("call_report_new")).context["form"]

        self.assertNotIn("employee", form.fields)

    def test_only_their_own_territory_is_offered(self):
        form = self.client.get(reverse("call_report_new")).context["form"]

        offered = list(form.fields["call_point"].queryset)

        self.assertIn(self.mine, offered)
        self.assertNotIn(self.theirs, offered)

    def test_a_visit_files_under_the_signed_in_member(self):
        self.report()

        self.assertEqual(CallReport.objects.get().employee, self.mr)

    def test_a_doctor_outside_their_patch_is_refused(self):
        response = self.report(call_point=self.theirs.pk)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(CallReport.objects.count(), 0)

    def test_a_new_call_point_lands_in_their_own_territory(self):
        self.report(call_point="", new_call_point="Dr. Sana Malik")

        created = CallPoint.objects.get(name="Dr. Sana Malik")

        self.assertEqual(created.territory, self.territory)

    def test_they_cannot_file_a_new_call_point_into_another_territory(self):
        response = self.report(
            call_point="", new_call_point="Dr. Sana Malik",
            new_call_point_territory=self.other_territory.pk,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            CallPoint.objects.filter(name="Dr. Sana Malik").count(), 0
        )

    # -------------------------------------------------------- the sample form

    def test_the_sample_form_does_not_ask_who_is_issuing(self):
        form = self.client.get(reverse("sample_new")).context["form"]

        self.assertNotIn("employee", form.fields)

        offered = list(form.fields["call_point"].queryset)

        self.assertIn(self.mine, offered)
        self.assertNotIn(self.theirs, offered)

    def test_samples_issue_under_the_signed_in_member(self):
        product = Product.objects.create(code="PAN500", name="Panadol")
        batch = Batch.objects.create(
            product=product, batch_no="B-1", quantity=100,
            expiry_date=timezone.localdate() + timedelta(days=365),
        )

        self.client.post(reverse("sample_new"), {
            "call_point": self.mine.pk,
            "date": timezone.localdate().isoformat(), "note": "",
            "batch[]": [str(batch.pk)], "qty[]": ["5"],
        })

        self.assertEqual(SampleIssue.objects.get().employee, self.mr)

    # --------------------------------------------------------- the office view

    def test_the_office_is_still_asked_who_the_visit_is_for(self):
        self.client.logout()
        User.objects.create_user("clerk", password="pw")
        self.client.login(username="clerk", password="pw")

        form = self.client.get(reverse("call_report_new")).context["form"]

        self.assertIn("employee", form.fields)

        offered = list(form.fields["call_point"].queryset)

        self.assertIn(self.mine, offered)
        self.assertIn(self.theirs, offered)

    def test_an_mr_with_no_territory_still_sees_every_call_point(self):
        """Better a full list than an empty one they cannot work from."""
        self.mr.territory = None
        self.mr.save()

        form = self.client.get(reverse("call_report_new")).context["form"]

        offered = list(form.fields["call_point"].queryset)

        self.assertIn(self.mine, offered)
        self.assertIn(self.theirs, offered)

    def test_the_day_offers_no_other_team_member_to_switch_to(self):
        response = self.client.get(reverse("daily_calls"))

        self.assertTrue(response.context["locked_to_me"])
        self.assertEqual(list(response.context["employees"]), [])
        self.assertEqual(response.context["employee"], self.mr)

    def test_the_office_can_still_switch_between_days_and_people(self):
        self.client.logout()
        User.objects.create_user("clerk", password="pw")
        self.client.login(username="clerk", password="pw")

        response = self.client.get(reverse("daily_calls"))

        self.assertFalse(response.context["locked_to_me"])
        self.assertIn(self.mr, response.context["employees"])
        self.assertIn(self.other, response.context["employees"])

    def test_the_schedule_page_shows_only_their_own_weeks(self):
        mine = WeeklyPlan.objects.create(
            employee=self.mr, week_start=monday_of(timezone.localdate())
        )
        WeeklyPlan.objects.create(
            employee=self.other, week_start=monday_of(timezone.localdate())
        )

        response = self.client.get(reverse("plan_list"))

        self.assertEqual(list(response.context["plans"]), [mine])

    def test_my_schedule_is_always_their_own_week(self):
        plan = WeeklyPlan.objects.create(
            employee=self.mr, week_start=monday_of(timezone.localdate())
        )
        PlanVisit.objects.create(plan=plan, call_point=self.mine, day=0)

        response = self.client.get(reverse("my_plan"))

        self.assertEqual(response.context["me"], self.mr)
        self.assertEqual(response.context["plan"], plan)
        self.assertEqual(response.context["days"][0]["visits"][0].call_point,
                         self.mine)


class ScheduleDayDetailTests(TestCase):
    """Each day of a plan shows how many calls were actually made."""

    def setUp(self):
        self.user = User.objects.create_user("clerk", password="pw")
        self.client.login(username="clerk", password="pw")

        self.territory = Territory.objects.create(name="Gulberg")
        self.mr = Employee.objects.create(
            employee_code="MR-01", full_name="Ali Raza", designation="mr",
            territory=self.territory,
        )

        self.monday = monday_of(timezone.localdate())
        self.plan = WeeklyPlan.objects.create(
            employee=self.mr, week_start=self.monday, status="approved"
        )

        self.doctors = [
            CallPoint.objects.create(
                name=f"Dr. {name}", territory=self.territory, kind="doctor"
            )
            for name in ("Ahmed", "Bilal", "Sana")
        ]

        for doctor in self.doctors[:2]:
            PlanVisit.objects.create(
                plan=self.plan, call_point=doctor, day=0
            )

    def days(self):
        response = self.client.get(reverse("plan_detail", args=[self.plan.pk]))

        return response.context["days"], response.context

    def report(self, doctor, day_offset=0, outcome=CallReport.MET, visit=None):
        return CallReport.objects.create(
            employee=self.mr, call_point=doctor, plan_visit=visit,
            visit_date=self.monday + timedelta(days=day_offset),
            outcome=outcome,
        )

    def test_a_day_with_nothing_reported_counts_zero(self):
        days, _ = self.days()

        self.assertEqual(days[0]["planned_count"], 2)
        self.assertEqual(days[0]["made_count"], 0)
        self.assertEqual(days[0]["coverage"], 0)

    def test_calls_made_are_counted_against_the_day_planned(self):
        self.report(self.doctors[0], visit=self.plan.visits.first())

        days, _ = self.days()

        self.assertEqual(days[0]["made_count"], 1)
        self.assertEqual(days[0]["met_count"], 1)
        self.assertEqual(days[0]["coverage"], 50)

    def test_an_unplanned_call_still_counts_towards_the_day(self):
        self.report(self.doctors[2])

        days, _ = self.days()

        self.assertEqual(days[0]["made_count"], 1)
        self.assertEqual(days[0]["unplanned_count"], 1)
        self.assertEqual(days[0]["reports"][0].call_point, self.doctors[2])

    def test_a_doctor_not_met_is_a_call_made_but_not_met(self):
        self.report(self.doctors[0], outcome=CallReport.NOT_AVAILABLE)

        days, _ = self.days()

        self.assertEqual(days[0]["made_count"], 1)
        self.assertEqual(days[0]["met_count"], 0)

    def test_each_call_lands_on_its_own_day(self):
        self.report(self.doctors[0], day_offset=0)
        self.report(self.doctors[1], day_offset=2)
        self.report(self.doctors[2], day_offset=2)

        days, _ = self.days()

        self.assertEqual(days[0]["made_count"], 1)
        self.assertEqual(days[1]["made_count"], 0)
        self.assertEqual(days[2]["made_count"], 2)

    def test_calls_outside_the_week_are_not_counted(self):
        self.report(self.doctors[0], day_offset=-3)
        self.report(self.doctors[1], day_offset=9)

        _, context = self.days()

        self.assertEqual(context["made_total"], 0)

    def test_another_members_calls_are_not_counted(self):
        other = Employee.objects.create(
            employee_code="MR-02", full_name="Sara", designation="mr",
        )
        CallReport.objects.create(
            employee=other, call_point=self.doctors[0], visit_date=self.monday
        )

        _, context = self.days()

        self.assertEqual(context["made_total"], 0)

    def test_samples_left_are_totalled_per_day_and_per_week(self):
        product = Product.objects.create(code="PAN", name="Panadol")
        batch = Batch.objects.create(
            product=product, batch_no="B-1", quantity=100,
            expiry_date=timezone.localdate() + timedelta(days=365),
        )

        report = self.report(self.doctors[0])
        report.sample_issue = SampleIssue.objects.create(
            employee=self.mr, call_point=self.doctors[0], date=self.monday
        )
        SampleIssueItem.objects.create(
            sample_issue=report.sample_issue, product=product,
            batch=batch, qty=6,
        )
        report.save()

        days, context = self.days()

        self.assertEqual(days[0]["samples"], 6)
        self.assertEqual(context["samples_total"], 6)

    def test_the_week_total_adds_the_days_up(self):
        self.report(self.doctors[0], day_offset=0)
        self.report(self.doctors[1], day_offset=1)
        self.report(self.doctors[2], day_offset=5)

        _, context = self.days()

        self.assertEqual(context["made_total"], 3)

    def test_the_mr_sees_the_same_breakdown_on_their_own_schedule(self):
        account = User.objects.create_user("ali", password="pw12345678")
        UserRolls.objects.filter(user=account).update(
            role=UserRolls.ROLE_FIELD
        )
        self.mr.user = account
        self.mr.save()

        self.report(self.doctors[0])

        self.client.logout()
        self.client.login(username="ali", password="pw12345678")

        response = self.client.get(reverse("my_plan"))

        self.assertEqual(response.context["days"][0]["made_count"], 1)
        self.assertEqual(response.context["made_total"], 1)

    def test_both_schedule_pages_render(self):
        self.report(self.doctors[2])

        for url in (reverse("plan_detail", args=[self.plan.pk]),
                    reverse("my_plan")):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)


class CheckSchemaCommandTests(TestCase):
    """The command that answers "why does saving 500 on the server?"."""

    def run_command(self, **kwargs):
        out = StringIO()
        call_command("check_schema", stdout=out, **kwargs)

        return out.getvalue()

    def test_a_migrated_database_reports_clean(self):
        output = self.run_command()

        self.assertIn("All migrations applied", output)
        self.assertIn("Schema matches the models", output)

    def test_a_missing_column_is_named(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE invoices_callpoint DROP COLUMN estimated_volume"
            )

        output = self.run_command()

        self.assertIn("missing column  invoices_callpoint.estimated_volume",
                      output)
        self.assertIn("python manage.py migrate", output)

    def test_a_missing_table_is_named(self):
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE invoices_callreport")

        output = self.run_command()

        self.assertIn("missing table   invoices_callreport", output)

    def test_it_names_the_database_it_looked_at(self):
        output = self.run_command()

        self.assertIn("Database:", output)
        self.assertIn(connection.settings_dict["ENGINE"].rsplit(".", 1)[-1],
                      output)


class BatchCorrectionNoteTests(TestCase):
    """A correction note must never be what breaks the correction."""

    def test_a_date_that_was_never_set_is_described_not_formatted(self):
        product = Product.objects.create(code="PAN", name="Panadol")
        batch = Batch.objects.create(
            product=product, batch_no="B-1", quantity=10,
            expiry_date=date(2028, 1, 31),
        )

        movement = record_batch_correction(batch, ("B-1", None), None)

        self.assertEqual(movement.quantity, 0)
        self.assertIn("not set → 31-01-2028", movement.note)

    def test_an_unchanged_batch_records_nothing(self):
        product = Product.objects.create(code="PAN", name="Panadol")
        batch = Batch.objects.create(
            product=product, batch_no="B-1", quantity=10,
            expiry_date=date(2028, 1, 31),
        )

        self.assertIsNone(
            record_batch_correction(batch, ("B-1", date(2028, 1, 31)), None)
        )
        self.assertEqual(StockMovement.objects.count(), 0)
