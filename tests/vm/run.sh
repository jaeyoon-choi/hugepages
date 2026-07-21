#!/bin/sh
# Run the FreeBSD VM scenario through the shared fbsdvm harness.
# The harness lives outside this repo; point VMTEST_DIR at it if it is not
# in the default location (a `vmtest` directory next to this repo).
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
VMTEST_DIR="${VMTEST_DIR:-$HERE/../../../vmtest}"
exec "$VMTEST_DIR/venv/bin/python" "$VMTEST_DIR/fbsdvm.py" run \
    --project "$HERE/../.." --guest-script tests/vm/freebsd.sh "$@"
