#!/usr/bin/env bash
#
# Adds the INTERNET permission to the Android release manifest.
#
# `flutter create` writes that permission into the debug and profile manifests
# only, never the main one. So a debug build has network and a release build
# silently does not - every request fails before it leaves the phone, and the
# app reports "No connection" on a device with full signal.
#
# android/ is not tracked in git (it is regenerated per machine and per SDK
# version), so this has to be re-applied after any `flutter create`. Running it
# twice is safe.

set -euo pipefail

cd "$(dirname "$0")/.."

MANIFEST="android/app/src/main/AndroidManifest.xml"

if [ ! -f "$MANIFEST" ]; then
  echo "No $MANIFEST — run 'flutter create --org com.lifemedpharmaceutical .' first." >&2
  exit 1
fi

if grep -q "android.permission.INTERNET" "$MANIFEST"; then
  echo "✓ INTERNET permission already present."
  exit 0
fi

python3 - "$MANIFEST" <<'PY'
import sys

path = sys.argv[1]
source = open(path).read()

marker = "<application"
permission = '    <uses-permission android:name="android.permission.INTERNET"/>\n\n'

if marker not in source:
    sys.exit(f"Could not find <application in {path}; add the permission by hand.")

# Before the first <application, which is where Android expects permissions.
head, _, tail = source.partition(marker)
open(path, "w").write(head + permission + marker + tail)
PY

echo "✓ Added INTERNET permission to $MANIFEST"
grep -n "INTERNET" "$MANIFEST"
