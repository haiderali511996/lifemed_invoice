"""Cap the Gradle JVM so the OS does not kill it mid-build.

Only rewrites a heap of 8G or more. A smaller setting is either the Flutter
default or somebody's deliberate choice, and neither wants overriding.
"""

import re
import sys

SAFE = (
    "org.gradle.jvmargs=-Xmx3G -XX:MaxMetaspaceSize=1G "
    "-XX:+HeapDumpOnOutOfMemoryError"
)

path = sys.argv[1]
source = open(path).read()

match = re.search(r"^org\.gradle\.jvmargs=.*$", source, flags=re.M)

if match is None:
    with open(path, "a") as handle:
        handle.write(f"\n{SAFE}\n")

    print(f"✓ Set the Gradle JVM to 3G in {path}")
    sys.exit(0)

oversized = re.search(r"-Xmx(\d+)G", match.group(0))

if oversized and int(oversized.group(1)) >= 8:
    open(path, "w").write(source.replace(match.group(0), SAFE))
    print(f"✓ Capped the Gradle JVM at 3G in {path} (was {oversized.group(0)})")
else:
    print("✓ Gradle heap already reasonable.")
