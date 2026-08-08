"""Compare the live database against the models and report what is missing.

Every "internal server error on save" this project has had so far came down to
the same thing: code deployed without `migrate`, so a column the code writes to
does not exist yet. The page loads, because reading it never touches the new
column, and the failure only appears on save - which reads as a mysterious bug
rather than a missing migration.

`python manage.py check_schema` says so in one line, on the server, without
needing to read a traceback out of a Passenger log.
"""

from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


class Command(BaseCommand):
    help = "Report tables and columns the models expect but the database lacks."

    def add_arguments(self, parser):
        parser.add_argument(
            "--app", default="invoices",
            help="Application label to check (default: invoices).",
        )

    def handle(self, *args, **options):
        app_label = options["app"]

        self.stdout.write(
            f"Database: {connection.settings_dict['ENGINE'].rsplit('.', 1)[-1]}"
            f" · {connection.settings_dict['NAME']}"
        )

        pending = self.unapplied_migrations()

        if pending:
            self.stdout.write(
                self.style.WARNING(
                    f"\n{len(pending)} migration(s) not applied:"
                )
            )

            for name in pending:
                self.stdout.write(f"  [ ] {name}")
        else:
            self.stdout.write(self.style.SUCCESS("\nAll migrations applied."))

        missing_tables, missing_columns = self.drift(app_label)

        if not missing_tables and not missing_columns:
            if pending:
                # Migrations that only add data, or that Django has not
                # recorded, leave the schema itself correct.
                self.stdout.write(
                    "\nThe schema itself matches the models, but run "
                    "`python manage.py migrate` to apply the above."
                )
            else:
                self.stdout.write(
                    self.style.SUCCESS(
                        "\nSchema matches the models. Nothing missing."
                    )
                )

            return

        self.stdout.write(self.style.ERROR("\nThe database is out of step:"))

        for table in missing_tables:
            self.stdout.write(self.style.ERROR(f"  missing table   {table}"))

        for table, column in missing_columns:
            self.stdout.write(
                self.style.ERROR(f"  missing column  {table}.{column}")
            )

        self.stdout.write(
            "\nThis is what causes a page to load but 500 on save. Fix it with:"
            "\n  python manage.py migrate"
            "\n  touch tmp/restart.txt"
        )

    def unapplied_migrations(self):
        executor = MigrationExecutor(connection)
        targets = executor.loader.graph.leaf_nodes()

        return [
            f"{migration.app_label}.{migration.name}"
            for migration, _ in executor.migration_plan(targets)
        ]

    def drift(self, app_label):
        """Tables and columns the models expect that the database has not got."""
        existing_tables = set(connection.introspection.table_names())

        missing_tables = []
        missing_columns = []

        for model in apps.get_app_config(app_label).get_models():
            table = model._meta.db_table

            if table not in existing_tables:
                missing_tables.append(table)
                continue

            with connection.cursor() as cursor:
                columns = {
                    column.name
                    for column in connection.introspection.get_table_description(
                        cursor, table
                    )
                }

            for field in model._meta.local_fields:
                if field.column not in columns:
                    missing_columns.append((table, field.column))

            # Many-to-many rows live in their own tables, which a half-applied
            # migration can leave behind just as easily.
            for field in model._meta.local_many_to_many:
                through = field.remote_field.through

                if through._meta.auto_created:
                    if through._meta.db_table not in existing_tables:
                        missing_tables.append(through._meta.db_table)

        return sorted(set(missing_tables)), sorted(set(missing_columns))
