#!/bin/sh
# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) Jaeyoon Choi <j_yoon.choi@samsung.com>
#
# Linux VM scenario for hugepages -- executed as root inside a Debian guest
# by the fbsdvm harness (--os linux). Reserves real pages and mounts a real
# hugetlbfs, which is why this runs in a throwaway VM and not on the host.

set -u

fail=0
note() { echo; echo "== $*"; }
ok()   { echo "ok: $*"; }
bad()  { echo "FAIL: $*"; fail=$((fail + 1)); }

HP="python3 src/hugepages/hugepages.py"
SYSFS=/sys/kernel/mm/hugepages/hugepages-2048kB

note "environment"
uname -sr
ls /sys/kernel/mm/hugepages
[ -d "$SYSFS" ] || { echo "no 2 MiB hugepage support in this kernel"; exit 1; }

note "--version"
$HP --version || bad "--version"

note "info reports sysfs state"
$HP info >/tmp/info.out 2>&1 || bad "info rc"
grep -q "Size: 2048kB" /tmp/info.out && ok "2 MiB size listed" || bad "2 MiB size"
NR=$(cat "$SYSFS/nr_hugepages")
grep -q "Total: $NR" /tmp/info.out && ok "Total matches sysfs ($NR)" || bad "Total mismatch"

note "setup reserves real pages"
$HP setup --size 2048 --count 16 >/tmp/setup.out 2>&1 || bad "setup rc"
[ "$(cat "$SYSFS/nr_hugepages")" = "16" ] && ok "sysfs shows 16 pages" || bad "reservation"
$HP info | grep -q "Total: 16" && ok "info shows 16 pages" || bad "info after setup"

note "setup --count 0 releases the pool without a failure report"
$HP setup --size 2048 --count 0 >/tmp/release.out 2>&1 || bad "release rc"
if grep -q "No hugepages were reserved" /tmp/release.out; then
    bad "release wrongly reported as a failed reservation"
else
    ok "release not reported as a failure"
fi
[ "$(cat "$SYSFS/nr_hugepages")" = "0" ] && ok "pool released" || bad "pool not released"

note "mount hugetlbfs"
$HP mount --mountpoint /mnt/huge >/tmp/mount.out 2>&1 || bad "mount rc"
mount | grep -q "on /mnt/huge type hugetlbfs" && ok "hugetlbfs mounted" || bad "not mounted"
grep -q "Mounted hugetlbfs at /mnt/huge" /tmp/mount.out && ok "mount reported" || bad "mount output"

note "failed mount is loud and exits non-zero"
$HP mount --mountpoint /mnt/huge2 --pagesize 3 >/tmp/badmount.out 2>&1
rc=$?
[ $rc -ne 0 ] && ok "failed mount exits non-zero (rc=$rc)" || bad "failed mount exited 0"
grep -q "Failed to mount" /tmp/badmount.out && ok "failure reported" || bad "failure message"

note "shell metacharacters in --mountpoint are inert"
$HP mount --mountpoint '/tmp/x; touch /tmp/pwned' >/dev/null 2>&1 || true
[ ! -e /tmp/pwned ] && ok "no command injection" || bad "injection executed"

note "--verbose traces sysfs writes with the # LEVEL: format"
$HP --verbose setup --size 2048 --count 4 >/tmp/verbose.out 2>&1 || bad "verbose rc"
grep -q "# INFO: /sys/kernel/mm/hugepages" /tmp/verbose.out \
    && ok "verbose trace present" || bad "verbose trace missing"
$HP setup --size 2048 --count 0 >/dev/null 2>&1

note "result"
if [ "$fail" -eq 0 ]; then
    echo "ALL CHECKS PASSED"
else
    echo "$fail CHECK(S) FAILED"
fi
exit "$fail"
