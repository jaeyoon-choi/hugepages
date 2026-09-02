#!/bin/sh
# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) Jaeyoon Choi <j_yoon.choi@samsung.com>
#
# Smoke test for the FreeBSD contigmem backend.
#
# The unit tests in test_cli.py fake every command. This script drives the
# real kenv, kldload, kldunload, and sysctl path on a live FreeBSD system.
# It needs root because it loads and unloads a kernel module. It restores
# the contigmem state it found before it exits.
#
# Usage: sh tests/freebsd-smoke.sh [path-to-hugepages]
#
# The path defaults to ../src/hugepages/hugepages.py next to this script.
# An installed "hugepages" also works: sh tests/freebsd-smoke.sh hugepages

set -u

HP=${1:-"$(dirname "$0")/../src/hugepages/hugepages.py"}
case "$HP" in
*.py) HP_CMD="python3 $HP" ;;
*) HP_CMD="$HP" ;;
esac

# 64 MiB buffers: large enough to be a real contiguous allocation, small
# enough to succeed on a fragmented test machine.
SIZE_KB=65536
SIZE_BYTES=$((SIZE_KB * 1024))

PASSED=0
FAILED=0
SKIPPED=0
TMPDIR_SMOKE=""
SAVED_LOADED=no
SAVED_COUNT=""
SAVED_SIZE=""

hp() {
	# Word splitting on HP_CMD is intended: it may carry the interpreter.
	# shellcheck disable=SC2086
	$HP_CMD "$@"
}

pass() {
	PASSED=$((PASSED + 1))
	printf 'ok       %s\n' "$1"
}

fail() {
	FAILED=$((FAILED + 1))
	printf 'FAIL     %s\n' "$1"
	printf '         %s\n' "$2"
}

skip() {
	SKIPPED=$((SKIPPED + 1))
	printf 'skip     %s\n' "$1"
	printf '         %s\n' "$2"
}

expect_status() { # label expected actual
	if [ "$2" = "$3" ]; then
		pass "$1"
	else
		fail "$1" "expected exit $2, got $3"
	fi
}

expect_fails() { # label actual
	if [ "$2" != "0" ]; then
		pass "$1"
	else
		fail "$1" "expected a non-zero exit, got 0"
	fi
}

expect_contains() { # label needle haystack
	case "$3" in
	*"$2"*) pass "$1" ;;
	*) fail "$1" "expected output to contain: $2" ;;
	esac
}

expect_equal() { # label expected actual
	if [ "$2" = "$3" ]; then
		pass "$1"
	else
		fail "$1" "expected '$2', got '$3'"
	fi
}

loaded() {
	kldstat -q -m contigmem
}

save_state() {
	if loaded; then
		SAVED_LOADED=yes
		SAVED_COUNT=$(sysctl -n hw.contigmem.num_buffers 2>/dev/null)
		SAVED_SIZE=$(sysctl -n hw.contigmem.buffer_size 2>/dev/null)
	fi
}

restore_state() {
	if loaded; then
		kldunload contigmem >/dev/null 2>&1
	fi
	if [ "$SAVED_LOADED" = yes ] && [ -n "$SAVED_COUNT" ] && [ -n "$SAVED_SIZE" ]; then
		kenv "hw.contigmem.num_buffers=$SAVED_COUNT" >/dev/null 2>&1
		kenv "hw.contigmem.buffer_size=$SAVED_SIZE" >/dev/null 2>&1
		kldload contigmem >/dev/null 2>&1 ||
			printf 'warning: could not restore the contigmem reservation\n' >&2
	fi
	[ -n "$TMPDIR_SMOKE" ] && rm -rf "$TMPDIR_SMOKE"
}

abort() {
	printf 'abort: %s\n' "$1" >&2
	exit 2
}

# --- guards ------------------------------------------------------------

[ "$(uname -s)" = "FreeBSD" ] || abort "this smoke test only runs on FreeBSD"
[ "$(id -u)" = "0" ] || abort "loading kernel modules needs root; re-run with sudo"
hp --version >/dev/null 2>&1 || abort "cannot run the tool: $HP_CMD"
[ -f /boot/modules/contigmem.ko ] || [ -f /boot/kernel/contigmem.ko ] ||
	abort "contigmem.ko not found; install DPDK (see: pkg search dpdk)"

save_state
trap restore_state EXIT INT TERM
TMPDIR_SMOKE=$(mktemp -d)

if loaded && ! kldunload contigmem >/dev/null 2>&1; then
	abort "contigmem is loaded and busy; stop the process holding it first"
fi

printf 'FreeBSD contigmem smoke test (%s buffers of %s kB)\n\n' 2 "$SIZE_KB"

# --- 1: info reports an unloaded module without failing ----------------

out=$(hp info 2>&1)
status=$?
expect_status "info exits 0 while contigmem is unloaded" 0 "$status"
expect_contains "info reports the module as not loaded" "contigmem: not loaded" "$out"
expect_contains "info names the package that ships it" "pkg search dpdk" "$out"

# --- 2: setup refuses to run as a plain user ---------------------------

if su -m nobody -c "$HP_CMD --version" >/dev/null 2>&1; then
	out=$(su -m nobody -c "$HP_CMD setup --count 1 --size $SIZE_KB" 2>&1)
	status=$?
	expect_fails "setup as a plain user fails" "$status"
	expect_contains "setup as a plain user asks for sudo" "Re-run with sudo" "$out"
else
	skip "setup as a plain user" "user nobody cannot run $HP_CMD here"
fi

# --- 3: setup reserves buffers -----------------------------------------

out=$(hp setup --count 1 --size "$SIZE_KB" 2>&1)
status=$?
expect_status "setup reserves one buffer" 0 "$status"
expect_contains "setup reports the reservation" "Reserved 1 x $SIZE_KB kB" "$out"
expect_contains "setup prints the loader.conf hint" 'contigmem_load="YES"' "$out"
expect_equal "the kernel holds one buffer" "1" "$(sysctl -n hw.contigmem.num_buffers)"
expect_equal "the kernel holds the requested size" "$SIZE_BYTES" \
	"$(sysctl -n hw.contigmem.buffer_size)"
if [ -c /dev/contigmem ]; then
	pass "the module created /dev/contigmem"
else
	fail "the module created /dev/contigmem" "missing device node"
fi

# --- 4: info reports the loaded module ---------------------------------

out=$(hp info 2>&1)
expect_contains "info reports the module as loaded" "contigmem: loaded" "$out"
expect_contains "info reports the buffer geometry" \
	"Buffers: 1 x $SIZE_BYTES bytes ($SIZE_KB kB)" "$out"
physaddr=$(printf '0x%x' "$(sysctl -n hw.contigmem.physaddr.0)")
expect_contains "info reports the physical address in hex" \
	"Buffer 0: physaddr $physaddr" "$out"

# --- 5: a resize reloads the module in the right order -----------------

out=$(hp --verbose setup --count 2 --size "$SIZE_KB" 2>&1)
status=$?
expect_status "setup resizes the reservation" 0 "$status"
expect_contains "the resize reports both buffers" "Reserved 2 x $SIZE_KB kB" "$out"

unload_at=$(printf '%s\n' "$out" | grep -n 'cmd(kldunload contigmem)' | head -1 | cut -d: -f1)
kenv_at=$(printf '%s\n' "$out" | grep -n 'cmd(kenv hw.contigmem.num_buffers' | head -1 | cut -d: -f1)
load_at=$(printf '%s\n' "$out" | grep -n 'cmd(kldload contigmem)' | head -1 | cut -d: -f1)
if [ -n "$unload_at" ] && [ -n "$kenv_at" ] && [ -n "$load_at" ] &&
	[ "$unload_at" -lt "$kenv_at" ] && [ "$kenv_at" -lt "$load_at" ]; then
	pass "the resize unloads, then sets the tunables, then loads"
else
	fail "the resize unloads, then sets the tunables, then loads" \
		"trace order was unload=$unload_at kenv=$kenv_at load=$load_at"
fi

# --- 6: an invalid size is rejected before anything runs ---------------

before=$(sysctl -n hw.contigmem.num_buffers)
out=$(hp setup --count 1 --size 1000 2>&1)
status=$?
expect_fails "setup rejects a size that is not a power of 2" "$status"
expect_contains "setup explains the power-of-2 rule" "power of 2" "$out"
expect_equal "the rejected setup left the reservation alone" "$before" \
	"$(sysctl -n hw.contigmem.num_buffers)"

# --- 7: a mapped buffer blocks the release -----------------------------

cat >"$TMPDIR_SMOKE/hold.py" <<EOF
import mmap, os, time

fd = os.open("/dev/contigmem", os.O_RDWR)
m = mmap.mmap(fd, $SIZE_BYTES, mmap.MAP_SHARED,
              mmap.PROT_READ | mmap.PROT_WRITE, offset=0)
m[0:4] = b"test"
time.sleep(120)
EOF
python3 "$TMPDIR_SMOKE/hold.py" &
holder=$!
sleep 2

if kill -0 "$holder" 2>/dev/null; then
	out=$(hp setup --count 0 2>&1)
	status=$?
	expect_fails "release fails while a process maps a buffer" "$status"
	expect_contains "the busy message names the mapping" "mapped" "$out"
	expect_contains "the busy message names fstat" "fstat /dev/contigmem" "$out"
	kill "$holder" 2>/dev/null
	wait "$holder" 2>/dev/null
	sleep 2
else
	skip "release fails while a process maps a buffer" "could not mmap /dev/contigmem"
fi

# --- 8: release, and releasing again ------------------------------------

out=$(hp setup --count 0 2>&1)
status=$?
expect_status "release unloads the module" 0 "$status"
expect_contains "release reports the unload" "Released" "$out"

out=$(hp setup --count 0 2>&1)
status=$?
expect_status "releasing an unloaded module exits 0" 0 "$status"
expect_contains "releasing an unloaded module says so" "nothing to release" "$out"

# --- summary ------------------------------------------------------------

printf '\n%d passed, %d failed, %d skipped\n' "$PASSED" "$FAILED" "$SKIPPED"
printf '\nNot covered here: a reboot with these lines in /boot/loader.conf\n'
printf '  hw.contigmem.num_buffers=1\n'
printf '  hw.contigmem.buffer_size=%d\n' "$SIZE_BYTES"
printf '  contigmem_load="YES"\n'

[ "$FAILED" = "0" ]
