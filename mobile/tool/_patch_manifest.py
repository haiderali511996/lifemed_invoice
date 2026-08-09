"""Add the INTERNET permission to an Android manifest, before <application."""

import re
import sys

path = sys.argv[1]
source = open(path).read()

if "android.permission.INTERNET" in source:
    sys.exit(0)

# Matched as a line so the existing indentation can be reused, rather than
# splitting on the bare tag and leaving the tag flush against the margin.
match = re.search(r"^([ \t]*)<application\b", source, flags=re.M)

if match is None:
    sys.exit(f"Could not find <application in {path}; add the permission by hand.")

indent = match.group(1)
permission = f'{indent}<uses-permission android:name="android.permission.INTERNET"/>\n\n'

open(path, "w").write(
    source[:match.start()] + permission + source[match.start():]
)
