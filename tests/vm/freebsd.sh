#!/bin/sh
# SPDX-License-Identifier: BSD-3-Clause
#
# FreeBSD VM scenario for hugepages -- executed as root inside the guest by
# the fbsdvm harness. Verifies the sysctl-backed superpage reporting against
# the kernel's own answers.

set -u

fail=0
note() { echo; echo "== $*"; }
ok()   { echo "ok: $*"; }
bad()  { echo "FAIL: $*"; fail=$((fail + 1)); }

HUGEPAGES="python3 src/hugepages/hugepages.py"

note "environment"
freebsd-version
sysctl vm.pmap.pg_ps_enabled hw.pagesizes 2>/dev/null || true

note "--version"
$HUGEPAGES --version || bad "--version"

note "info reports superpage state matching sysctl"
$HUGEPAGES info >/tmp/info.out 2>&1 || bad "info rc"
truth=$(sysctl -n vm.pmap.pg_ps_enabled 2>/dev/null || echo "")
case "$truth" in
    1) grep -q "Superpages: enabled" /tmp/info.out \
           && ok "enabled state matches sysctl" || bad "enabled state" ;;
    0) grep -q "Superpages: disabled" /tmp/info.out \
           && ok "disabled state matches sysctl" || bad "disabled state" ;;
    *) grep -q "Superpages: " /tmp/info.out \
           && ok "state reported (arm64-style oid)" || bad "no superpage state" ;;
esac
if sysctl -n hw.pagesizes | grep -q 2097152; then
    grep -q "Page size: 2097152 bytes (2048 kB)" /tmp/info.out \
        && ok "2 MiB page size reported" || bad "2 MiB page size"
fi
grep -q "superpage mappings" /tmp/info.out && ok "pde/l2 stats reported" || bad "pde/l2 stats"
grep -q "promotions:" /tmp/info.out && ok "promotions counter reported" || bad "promotions counter"

note "--verbose traces sysctl commands"
# run() logs each command at INFO while the backend probes sysctl. If anything
# logs before main() configures logging, the root logger is implicitly
# configured and basicConfig() becomes a no-op, silently dropping both the
# trace and the "# LEVEL:" prefix.
$HUGEPAGES --verbose info >/tmp/verbose.out 2>&1 || bad "verbose rc"
grep -q "# INFO: cmd(sysctl" /tmp/verbose.out \
    && ok "verbose traces sysctl with the # LEVEL: format" || bad "verbose trace missing"

note "setup is informational and needs no --count on FreeBSD"
$HUGEPAGES setup >/tmp/setup.out 2>&1
rc=$?
[ $rc -eq 0 ] && ok "setup exits 0 without arguments" || bad "setup rc=$rc"
grep -q "reservation-based" /tmp/setup.out && ok "setup explains superpages" || bad "setup text"
grep -q "Superpages are currently" /tmp/setup.out && ok "setup reports current state" || bad "setup state"

note "mount is informational"
$HUGEPAGES mount >/tmp/mount.out 2>&1
rc=$?
[ $rc -eq 0 ] && ok "mount exits 0" || bad "mount rc=$rc"
grep -q "no hugetlbfs" /tmp/mount.out && ok "mount explains MAP_ALIGNED_SUPER" || bad "mount text"

note "no command errors cleanly"
$HUGEPAGES >/tmp/nocmd.out 2>&1
rc=$?
[ $rc -eq 1 ] && grep -q "No command specified" /tmp/nocmd.out \
    && ok "missing command -> rc 1" || bad "missing command rc=$rc"

note "result"
if [ "$fail" -eq 0 ]; then
    echo "ALL CHECKS PASSED"
else
    echo "$fail CHECK(S) FAILED"
fi
exit "$fail"
