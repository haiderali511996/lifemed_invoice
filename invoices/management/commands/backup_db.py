"""Dump the database to a compressed file, and prune old dumps.

Intended to run from cPanel's cron scheduler. Produces gzipped SQL from either
MySQL (via mysqldump) or SQLite, so it behaves the same in development and in
production.

    python manage.py backup_db
    python manage.py backup_db --keep-days 30 --output-dir ~/backups
"""

import gzip
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

DEFAULT_KEEP_DAYS = 14
BACKUP_SUFFIX = ".sql.gz"


class Command(BaseCommand):
    help = "Back up the database to a timestamped, compressed file."

    def add_arguments(self, parser):
        parser.add_argument(
            "--output-dir",
            default=os.getenv("BACKUP_DIR", str(Path(settings.BASE_DIR) / "backups")),
            help="Where to write dumps. Defaults to BACKUP_DIR or ./backups.",
        )
        parser.add_argument(
            "--keep-days",
            type=int,
            default=int(os.getenv("BACKUP_KEEP_DAYS", DEFAULT_KEEP_DAYS)),
            help=f"Delete dumps older than this. Default {DEFAULT_KEEP_DAYS}.",
        )
        parser.add_argument(
            "--quiet",
            action="store_true",
            help="Only report problems. Use this from cron to avoid daily mail.",
        )

    def handle(self, *args, **options):
        output_dir = Path(options["output_dir"]).expanduser()
        output_dir.mkdir(parents=True, exist_ok=True)

        # Backups contain every customer record and password hash; keep the
        # directory private even if it ends up somewhere web-accessible.
        os.chmod(output_dir, 0o700)

        config = settings.DATABASES["default"]
        engine = config["ENGINE"]

        target = self._unique_target(output_dir)

        if "sqlite" in engine:
            self._dump_sqlite(config, target)

        elif "mysql" in engine:
            self._dump_mysql(config, target)

        else:
            raise CommandError(f"No backup routine for engine {engine!r}.")

        os.chmod(target, 0o600)

        removed = self._prune(output_dir, options["keep_days"])

        if not options["quiet"]:
            size_mb = target.stat().st_size / (1024 * 1024)

            self.stdout.write(
                self.style.SUCCESS(
                    f"Wrote {target} ({size_mb:.2f} MB); pruned {removed} old dump(s)."
                )
            )

    def _unique_target(self, output_dir):
        """Timestamped path, suffixed if one already exists.

        The stamp is per-second, so a manual run right after a scheduled one
        would otherwise overwrite that day's backup.
        """
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

        target = output_dir / f"backup-{stamp}{BACKUP_SUFFIX}"
        attempt = 2

        while target.exists():
            target = output_dir / f"backup-{stamp}-{attempt}{BACKUP_SUFFIX}"
            attempt += 1

        return target

    # ---------------------------------------------------------------- engines

    def _dump_sqlite(self, config, target):
        """Dump SQL text through the live connection.

        Deliberately not a file copy (which can catch the database mid-write)
        and not sqlite3's backup API (which blocks on any open write
        transaction). iterdump reads through the existing connection, so it
        works while the application is running.
        """
        from django.db import connection

        connection.ensure_connection()

        with gzip.open(target, "wt", encoding="utf-8") as out:
            for statement in connection.connection.iterdump():
                out.write(f"{statement}\n")

    def _dump_mysql(self, config, target):
        if shutil.which("mysqldump") is None:
            raise CommandError(
                "mysqldump is not on PATH. On cPanel it usually lives in "
                "/usr/bin; ask your host if it is missing."
            )

        # The password goes in a 0600 options file rather than on the command
        # line, where it would be visible to anyone running `ps`.
        handle, defaults_path = tempfile.mkstemp(prefix="backup-", suffix=".cnf")

        try:
            with os.fdopen(handle, "w") as cnf:
                cnf.write("[client]\n")
                cnf.write(f"user={config['USER']}\n")
                cnf.write(f"password={config['PASSWORD']}\n")
                cnf.write(f"host={config['HOST'] or 'localhost'}\n")

                if config.get("PORT"):
                    cnf.write(f"port={config['PORT']}\n")

            os.chmod(defaults_path, 0o600)

            command = [
                "mysqldump",
                f"--defaults-extra-file={defaults_path}",
                "--single-transaction",
                "--quick",
                "--default-character-set=utf8mb4",
                config["NAME"],
            ]

            with gzip.open(target, "wb") as out:
                result = subprocess.run(
                    command, stdout=out, stderr=subprocess.PIPE, check=False
                )

            if result.returncode != 0:
                target.unlink(missing_ok=True)

                raise CommandError(
                    "mysqldump failed: "
                    + result.stderr.decode(errors="replace").strip()
                )

        finally:
            os.unlink(defaults_path)

        if target.stat().st_size == 0:
            target.unlink(missing_ok=True)

            raise CommandError("mysqldump produced an empty file.")

    # ---------------------------------------------------------------- pruning

    def _prune(self, output_dir, keep_days):
        if keep_days <= 0:
            return 0

        cutoff = datetime.now() - timedelta(days=keep_days)
        removed = 0

        for path in output_dir.glob("backup-*"):
            if not path.is_file():
                continue

            if datetime.fromtimestamp(path.stat().st_mtime) < cutoff:
                path.unlink()
                removed += 1

        return removed
