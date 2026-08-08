"""Give every existing doctor-kind call point a doctor record of its own.

The call points imported from the territory spreadsheet are named as people
("Dr. Ahmed"), because until now the call point *was* the doctor. Splitting
places from people leaves those rows meaning both at once.

This creates one doctor per doctor-kind call point, carrying the same name and
speciality, and leaves the call point exactly as it was - so nothing breaks,
past visits keep pointing where they always did, and the place can be renamed
to what it actually is ("Shifa Clinic") whenever somebody gets round to it.

Chemists and hospitals are left alone: those are places already, and inventing
a doctor called "City Pharmacy" would be worse than having none.
"""

from django.db import migrations


def create_doctors(apps, schema_editor):
    CallPoint = apps.get_model("invoices", "CallPoint")
    Doctor = apps.get_model("invoices", "Doctor")

    made = []

    for call_point in CallPoint.objects.filter(kind="doctor"):
        # get_or_create so a re-run, or a database where somebody has already
        # added the doctor by hand, does not end up with two of them.
        if Doctor.objects.filter(
            name=call_point.name, call_point=call_point
        ).exists():
            continue

        made.append(Doctor(
            name=call_point.name,
            speciality=call_point.speciality or "",
            call_point=call_point,
            phone=call_point.phone or "",
            is_active=call_point.is_active,
        ))

    Doctor.objects.bulk_create(made)


def remove_doctors(apps, schema_editor):
    """Undo only what this migration made: a doctor named after its own place.

    A doctor added since, or moved here from somewhere else, has a different
    name from the call point and is left alone.
    """
    Doctor = apps.get_model("invoices", "Doctor")

    for doctor in Doctor.objects.select_related("call_point"):
        if doctor.call_point.kind == "doctor" and doctor.name == doctor.call_point.name:
            doctor.delete()


class Migration(migrations.Migration):

    dependencies = [
        ("invoices", "0018_doctor_target_doctormove_producttarget"),
    ]

    operations = [
        migrations.RunPython(create_doctors, remove_doctors),
    ]
