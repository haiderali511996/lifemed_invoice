"""Load territories and their call points from a CSV.

Ships with invoices/data/territories.csv (the Lahore zone breakdown). Safe to
re-run: records are matched on territory code and on call-point name within a
territory, so existing rows are updated rather than duplicated.

    python manage.py import_territories
    python manage.py import_territories --file other.csv --dry-run
"""

import csv
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from invoices.models import CallPoint, Territory

DEFAULT_FILE = "invoices/data/territories.csv"

REQUIRED_COLUMNS = {
    "zone", "territory_code", "territory_name", "city", "call_point",
}

# Names hint at what kind of call point it is; an MR's day differs at a
# wholesale market versus a teaching hospital.
CHEMIST_HINTS = ("market", "chemist", "pharmacy", "medical store")
HOSPITAL_HINTS = (
    "hospital", "medical centre", "medical center", "medical complex",
    "university", "trust", "imc", "cmh", "complex",
)


def classify(name):
    lowered = name.lower()

    if any(hint in lowered for hint in CHEMIST_HINTS):
        return "chemist"

    if any(hint in lowered for hint in HOSPITAL_HINTS):
        return "hospital"

    # Clinics, GP clusters and networks are doctor call points.
    return "doctor"


class Command(BaseCommand):
    help = "Import territories and call points from a CSV file."

    def add_arguments(self, parser):
        parser.add_argument(
            "--file",
            default=str(Path(settings.BASE_DIR) / DEFAULT_FILE),
            help=f"CSV to read. Defaults to {DEFAULT_FILE}.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing anything.",
        )

    def handle(self, *args, **options):
        path = Path(options["file"]).expanduser()

        if not path.exists():
            raise CommandError(f"{path} does not exist.")

        with open(path, newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))

        if not rows:
            raise CommandError("The file has no data rows.")

        missing = REQUIRED_COLUMNS - set(rows[0])

        if missing:
            raise CommandError(
                "Missing column(s): " + ", ".join(sorted(missing))
            )

        try:
            with transaction.atomic():
                counts = self._import(rows)

                if options["dry_run"]:
                    # Roll back so a dry run cannot leave anything behind.
                    raise _DryRun(counts)

        except _DryRun as preview:
            counts = preview.counts

            self.stdout.write(self.style.WARNING("Dry run - nothing written."))

        self.stdout.write(self.style.SUCCESS(
            f"Territories: {counts['territories_created']} created, "
            f"{counts['territories_updated']} updated. "
            f"Call points: {counts['call_points_created']} created, "
            f"{counts['call_points_updated']} updated."
        ))

        if counts["skipped"]:
            self.stdout.write(self.style.WARNING(
                f"Skipped {counts['skipped']} row(s) with no territory or name."
            ))

    def _import(self, rows):
        counts = {
            "territories_created": 0, "territories_updated": 0,
            "call_points_created": 0, "call_points_updated": 0, "skipped": 0,
        }

        territories = {}

        for row in rows:
            code = (row.get("territory_code") or "").strip()
            name = (row.get("territory_name") or "").strip()

            if not code and not name:
                counts["skipped"] += 1
                continue

            if code not in territories:
                territories[code] = self._territory(row, code, name, counts)

            self._call_point(row, territories[code], counts)

        return counts

    def _territory(self, row, code, name, counts):
        zone = (row.get("zone") or "").strip()
        city = (row.get("city") or "").strip()

        # Match on code where there is one; the name is the fallback key.
        existing = (
            Territory.objects.filter(code=code).first() if code
            else Territory.objects.filter(name=name).first()
        )

        if existing is None:
            existing = Territory.objects.filter(name=name).first()

        if existing is None:
            counts["territories_created"] += 1

            return Territory.objects.create(
                code=code, name=name, city=city, region=zone, is_active=True
            )

        changed = []

        for field, value in (("code", code), ("name", name),
                             ("city", city), ("region", zone)):
            if value and getattr(existing, field) != value:
                setattr(existing, field, value)
                changed.append(field)

        if changed:
            existing.save(update_fields=changed)
            counts["territories_updated"] += 1

        return existing

    def _call_point(self, row, territory, counts):
        name = (row.get("call_point") or "").strip()

        if not name:
            counts["skipped"] += 1
            return

        address = (row.get("address") or "").strip()
        volume = (row.get("estimated_volume") or "").strip()

        call_point, created = CallPoint.objects.get_or_create(
            name=name,
            territory=territory,
            defaults={
                "kind": classify(name),
                "address": address,
                "estimated_volume": volume,
                "is_active": True,
            },
        )

        if created:
            counts["call_points_created"] += 1
            return

        changed = []

        for field, value in (("address", address),
                             ("estimated_volume", volume)):
            if value and getattr(call_point, field) != value:
                setattr(call_point, field, value)
                changed.append(field)

        if changed:
            call_point.save(update_fields=changed)
            counts["call_points_updated"] += 1


class _DryRun(Exception):
    """Unwinds the transaction after a preview."""

    def __init__(self, counts):
        super().__init__("dry run")
        self.counts = counts
