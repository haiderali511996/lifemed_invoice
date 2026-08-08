#!/usr/bin/env bash
#
# Post-`flutter create` fixes for the Android project.
#
# android/ is not tracked in git — it is regenerated per machine and per SDK
# version — so these have to be re-applied after any `flutter create`. Running
# this twice is safe.

set -euo pipefail

cd "$(dirname "$0")/.."

# ------------------------------------------------------------ INTERNET
#
# `flutter create` writes this permission into the debug and profile manifests
# only, never the main one. So a debug build has network and a release build
# silently does not: every request fails before it leaves the phone, and the
# app reports "No connection" on a device with full signal.

MANIFEST="android/app/src/main/AndroidManifest.xml"

if [ ! -f "$MANIFEST" ]; then
  echo "No $MANIFEST — run 'flutter create --org com.lifemedpharmaceutical .' first." >&2
  exit 1
fi

if grep -q "android.permission.INTERNET" "$MANIFEST"; then
  echo "✓ INTERNET permission already present."
else
  python3 tool/_patch_manifest.py "$MANIFEST"
  echo "✓ Added INTERNET permission to $MANIFEST"
fi

# --------------------------------------------------------- Gradle heap
#
# Recent Flutter templates ask Gradle for -Xmx8G with a 4G metaspace. On a
# machine with 8 or 16 GB that reservation exceeds what is actually free once
# the OS, the IDE and the Dart compiler have taken theirs, and the JVM is
# killed mid-build. Gradle reports that as "build daemon disappeared
# unexpectedly", which reads like a crash rather than the memory problem it is.
#
# 3 GB builds this app comfortably.

PROPERTIES="android/gradle.properties"

if [ -f "$PROPERTIES" ]; then
  python3 tool/_patch_gradle_memory.py "$PROPERTIES"
fi

echo
echo "Done. Now: flutter pub get && flutter build apk --release"
