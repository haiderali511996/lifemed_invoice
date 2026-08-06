"""Entry point for Phusion Passenger, used by cPanel's "Setup Python App".

cPanel looks for a `passenger_wsgi.py` in the application root that exposes a
WSGI callable named `application`. Point the app's "Application startup file"
at this file and its "Application Entry point" at `application`.
"""

import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Passenger does not guarantee the project root is importable.
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'invoice_pharma.settings')

from django.core.wsgi import get_wsgi_application  # noqa: E402

application = get_wsgi_application()
