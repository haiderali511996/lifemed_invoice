# Deploying to cPanel (Setup Python App)

Target: `http://invoice.lifemedpharmaceutical.com`

cPanel serves Python apps through Phusion Passenger, not gunicorn. The entry
point is `passenger_wsgi.py` in the project root.

---

## 1. Create the subdomain

cPanel → **Domains** → *Create A Domain* → `invoice.lifemedpharmaceutical.com`.

Note the document root it creates (usually `/home/<cpuser>/invoice.lifemedpharmaceutical.com`).
The application code does **not** go there — Passenger serves it from a separate
app directory and links `public` into the document root automatically.

## 2. Create the MySQL database

cPanel → **MySQL Databases**:

1. Create a database, e.g. `invoicedb` → becomes `cpuser_invoicedb`
2. Create a user, e.g. `invoice` → becomes `cpuser_invoice`
3. Add the user to the database with **ALL PRIVILEGES**

Keep the full prefixed names and the password — they go into `DATABASE_URL`.

## 3. Create the Python application

cPanel → **Setup Python App** → *Create Application*:

| Field | Value |
|---|---|
| Python version | any 3.9+ (3.11 and 3.13 both tested) |
| Application root | `lifemed_invoice` |
| Application URL | `invoice.lifemedpharmaceutical.com` |
| Application startup file | `passenger_wsgi.py` |
| Application Entry point | `application` |

Click **Create**. cPanel prints a `source /home/<cpuser>/virtualenv/.../activate`
command at the top of the page — copy it, you need it in step 5.

## 4. Upload the code

Creating the app already put a stub `passenger_wsgi.py` and a `tmp/` folder in
`~/lifemed_invoice`, so `git clone <url> .` fails there with *"destination path
already exists and is not an empty directory"*. Clone next to it and move the
files in, overwriting the stub:

```bash
cd ~
git clone https://github.com/haiderali511996/lifemed_invoice.git invoice_src
cp -rf invoice_src/. lifemed_invoice/
rm -rf invoice_src
```

No Terminal on your plan? Download the repo zip from GitHub, upload it through
**File Manager**, extract it, and move the contents into `~/lifemed_invoice`,
replacing `passenger_wsgi.py` when asked.

Check that `manage.py`, `passenger_wsgi.py` and `template.pdf` all sit directly
in `~/lifemed_invoice`. The PDF generator reads `template.pdf` from the project
root and returns a 500 without it.

## 5. Create the `.env` file

In `~/lifemed_invoice`, copy `.env.example` to `.env` and fill it in. In **File
Manager** you must turn on *Settings → Show Hidden Files (dotfiles)* first,
otherwise `.env` and `.env.example` are invisible.

```ini
SECRET_KEY=<paste a generated key>
DEBUG=False
ALLOWED_HOSTS=invoice.lifemedpharmaceutical.com
CSRF_TRUSTED_ORIGINS=http://invoice.lifemedpharmaceutical.com,https://invoice.lifemedpharmaceutical.com
SECURE_COOKIES=False
USE_X_FORWARDED_PROTO=False
DATABASE_URL=mysql://cpuser_invoice:PASSWORD@localhost:3306/cpuser_invoicedb
```

> **`SECURE_COOKIES` must be `False` while the site is on `http://`.** Secure
> cookies are discarded by the browser over plain HTTP, so the login form would
> submit and bounce straight back with no error message. See step 8.

Generate the secret key with:

```bash
python -c "from django.core.management.utils import get_random_secret_key as k; print(k())"
```

If the DB password contains `@ : / # ?`, percent-encode it in the URL
(`@` → `%40`, `#` → `%23`).

## 6. Install and migrate

Activate the virtualenv using the command cPanel gave you in step 3, then:

```bash
cd ~/lifemed_invoice
pip install -r requirements.txt
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py createsuperuser
```

`migrate` also backfills user roles: the previous hardcoded super-admin account
and any Django superuser are promoted to the `super_admin` role automatically.

**Without Terminal access**, do the same from the *Setup Python App* page:
put `requirements.txt` in the **Configuration files** field and click *Run Pip
Install*, then use the **Execute python script** box for `manage.py migrate` and
`manage.py collectstatic --noinput`. That box cannot answer the interactive
prompts of `createsuperuser`, so create the first account with:

```
manage.py shell -c "from django.contrib.auth.models import User; User.objects.create_superuser('admin', 'you@example.com', 'a-strong-password')"
```

Then sign in at `/admin/` and change that password immediately.

## 6b. Import data from the old Neon database (optional)

Postgres and MySQL dumps are not interchangeable, so the data moves through
Django's serializer instead of raw SQL.

Export from Neon (the shell-level `DATABASE_URL` overrides `.env`):

```bash
cd ~/lifemed_invoice
pip install psycopg2-binary
DATABASE_URL='postgresql://USER:PASSWORD@HOST/DBNAME?sslmode=require' \
  python manage.py dumpdata auth.user invoices \
  --natural-foreign --natural-primary --indent 2 -o neon_backup.json
```

Import into the freshly migrated MySQL database:

```bash
python manage.py migrate
python manage.py loaddata neon_backup.json
```

`contenttypes` and `auth.permission` are deliberately not exported — Django
recreates them during `migrate`, and importing them causes primary-key clashes.

If the cPanel host blocks outbound port 5432, run the `dumpdata` step on a
machine that can reach Neon and upload the resulting JSON file.

## 7. Restart

cPanel → **Setup Python App** → **Restart** on the application.

Passenger caches the loaded code, so **every code or `.env` change needs a
restart**. From the shell, `touch tmp/restart.txt` in the app root does the same
thing.

## 7b. Schedule database backups

Everything lives in one MySQL database. Set this up before the ledgers hold
real money.

cPanel → **Cron Jobs** → add a job. Common Settings: **Once Per Day**
(or twice daily), and this command — one line, with your own paths:

```
/home/<cpuser>/virtualenv/lifemed_invoice/3.13/bin/python /home/<cpuser>/lifemed_invoice/manage.py backup_db --quiet
```

Use the same interpreter path cPanel gave you in step 3, with `/python`
appended in place of `/activate`.

Dumps land in `~/lifemed_invoice/backups/` as gzipped SQL, named
`backup-YYYYMMDD-HHMMSS.sql.gz`, owned 0600 in a 0700 directory. Anything
older than 14 days is pruned automatically.

Tune it in `.env`:

```ini
BACKUP_DIR=/home/<cpuser>/backups
BACKUP_KEEP_DAYS=30
```

Test it once by hand before trusting the schedule:

```bash
python manage.py backup_db
```

`--quiet` in the cron command suppresses success output so cPanel does not
email you daily; failures still report, which is what you want to hear about.

**Restoring:**

```bash
gunzip -c backups/backup-20260806-020000.sql.gz | mysql -u cpuser_dbuser -p cpuser_dbname
```

⚠️ A backup you have never restored is not a backup. Try one restore into a
scratch database now, while nothing depends on it. Backups sitting on the same
server as the database also die with the server — download a copy periodically,
or point `BACKUP_DIR` somewhere that is included in your host's off-site backup.

## 7c. Adding a distributor

Each distributor invoices on their own pre-printed form. Print positions are
read from the PDF rather than measured by hand.

1. **Distributors → New Distributor**
2. Fill in the name and a **code** — this becomes the invoice prefix, and each
   distributor keeps an independent number series (`HHC-9965`, `ODC-0001`).
3. Upload their blank invoice **PDF** and save.
4. The coordinates are read automatically; the confirmation says how many
   fields, columns and totals were found, and how many item rows fit per page.
5. **Open the preview** before invoicing. It prints sample data in every
   detected position so you can confirm the mapping on the real form.

The detector works by locating the labels the form already prints — "Customer
Name", "Invoice Number", the column headings, "Net Payable" — and placing each
value relative to its label. Two consequences worth knowing:

- The PDF must contain **real text**. A scanned image has no labels to find and
  will be rejected with a clear message.
- If a field is not found, it is listed on the layout page and left blank on
  the invoice. That is normal when the form has no such box.

## 8. Enable SSL (recommended)

cPanel → **SSL/TLS Status** → select the subdomain → **Run AutoSSL**.

Once the certificate is issued and `https://invoice.lifemedpharmaceutical.com`
loads, flip both flags in `.env` and restart:

```ini
SECURE_COOKIES=True
USE_X_FORWARDED_PROTO=True
```

Login credentials and generated invoices travel in plaintext until you do this.

---

## Managing roles

Roles live in the Django admin at `/admin/` → **User rolls**:

- `super_admin` — sees the activity log at `/logs/`, cannot generate invoices
- `manager` — sees the invoice form, cannot see the log

Every new user is created as `manager` automatically. Django superusers always
count as super admins.

---

## Troubleshooting

### A page loads but 500s when you press Save

This is almost always the database being a step behind the code: the new
column exists in the models but not in MySQL. Opening the page never reads it,
so only saving fails — which looks like a bug in the page rather than a missing
migration.

Ask the server directly:

```bash
cd ~/lifemed_invoice
source /home/haidersirat/virtualenv/lifemed_invoice/3.13/bin/activate
python manage.py check_schema
```

It prints any unapplied migrations and names every table and column the code
expects but the database has not got. If it reports drift:

```bash
python manage.py migrate
touch tmp/restart.txt
```

If `check_schema` says the schema is clean, the 500 is a real bug — get the
traceback with:

```bash
tail -n 100 ~/lifemed_invoice/stderr.log
```

### Everything else

| Symptom | Cause |
|---|---|
| 500 on every page, no Django output | Passenger failed to boot — read `~/lifemed_invoice/stderr.log` and the cPanel error log |
| Page loads, 500 on save | Run `python manage.py check_schema` — see above |
| Every page 500s right after a deploy | A package in `requirements.txt` did not install. Re-run `pip install -r requirements.txt` and read its output — a version conflict leaves the old code running against a settings file that imports something absent |
| `ImproperlyConfigured: SECRET_KEY is not set` | `.env` missing, in the wrong directory, or unreadable |
| `DisallowedHost` | Hostname missing from `ALLOWED_HOSTS` |
| 403 CSRF on *Generate PDF* | Origin missing from `CSRF_TRUSTED_ORIGINS`, or `SECURE_COOKIES=True` on http |
| Login form reloads with no error | `SECURE_COOKIES=True` while the site is on http |
| `No module named 'django'` / `'MySQLdb'` | `pip install -r requirements.txt` did not run in the app's virtualenv — check the prompt shows `(appname:version)` |
| PyMuPDF tries to compile from source | The pinned version has no wheel for your Python; upgrade the `PyMuPDF` pin |
| Admin pages unstyled | `collectstatic` was not run |
| Changes not taking effect | App not restarted |
| `FileNotFoundError: template.pdf` | `template.pdf` missing from the project root |

Set `DEBUG=True` temporarily to see a real traceback in the browser, then set it
back to `False` — it leaks settings and source code otherwise.
