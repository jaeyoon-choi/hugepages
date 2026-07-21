#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) Simon Andreas Frimann Lund <os@safl.dk>
#
# Tool for inspecting and configuring hugepages on Linux and FreeBSD
#
import argparse
import errno
import logging as log
import os
import platform
import re
import shlex
import subprocess
import sys
from pathlib import Path

__version__ = "0.2.10"

SYSFS_HUGEPAGES = Path("/sys/kernel/mm/hugepages")

BASH_COMPLETION = r"""# bash completion for hugepages
_hugepages() {
    local cur="${COMP_WORDS[COMP_CWORD]}"
    local prev="${COMP_WORDS[COMP_CWORD-1]}"
    local subcommands="info setup mount"
    local global_opts="--verbose --version --help --print-completion"
    case "${prev}" in
        --mountpoint)
            compopt -o default 2>/dev/null
            return 0
            ;;
    esac
    local cmd=""
    local i
    for ((i = 1; i < COMP_CWORD; i++)); do
        case "${COMP_WORDS[i]}" in
            info|setup|mount) cmd="${COMP_WORDS[i]}"; break ;;
        esac
    done
    case "${cmd}" in
        setup) COMPREPLY=($(compgen -W "--size --count ${global_opts}" -- "${cur}")) ;;
        mount) COMPREPLY=($(compgen -W "--mountpoint --pagesize ${global_opts}" -- "${cur}")) ;;
        info)  COMPREPLY=($(compgen -W "${global_opts}" -- "${cur}")) ;;
        *)     COMPREPLY=($(compgen -W "${subcommands} ${global_opts}" -- "${cur}")) ;;
    esac
}
complete -F _hugepages hugepages
"""


def run(cmd: list):
    """Run a command, without a shell, and capture the output"""

    log.info(f"cmd({shlex.join(cmd)})")
    return subprocess.run(cmd, capture_output=True, text=True)


def sysfs_write(path: Path, text):
    log.info(f'{path} "{text}"')
    with os.fdopen(os.open(path, os.O_WRONLY), "w") as f:
        return f.write(f"{text}\n")


class LinuxBackend:
    """Hugepage management on Linux via sysfs and hugetlbfs

    A backend provides ``info``/``setup``/``mount`` plus a ``configurable``
    flag. Only configurable backends -- those whose ``setup`` really reserves
    pages -- take a --count and provide ``supported_sizes()`` to enumerate the
    --size choices; see parse_args().
    """

    configurable = True

    def supported_sizes(self):
        sizes = []
        for entry in SYSFS_HUGEPAGES.glob("hugepages-*kB"):
            sizes.append(entry.name.split("-")[1].replace("kB", ""))
        return sizes

    def info(self, args):
        print("Hugepage Support:")
        for entry in SYSFS_HUGEPAGES.glob("hugepages-*kB"):
            size = entry.name.split("-")[1]
            nr = int((entry / "nr_hugepages").read_text())
            free = int((entry / "free_hugepages").read_text())
            resv = int((entry / "resv_hugepages").read_text())
            print(f"  Size: {size}  Total: {nr}  Free: {free}  Reserved: {resv}")

    def setup(self, args):
        """Setup hugepages via sysfs"""

        target = SYSFS_HUGEPAGES / f"hugepages-{args.size}kB" / "nr_hugepages"
        if not target.exists():
            log.error(f"Invalid hugepage size: {args.size}kB")
            sys.exit(1)

        try:
            sysfs_write(target, str(args.count))
        except PermissionError:
            log.error("Reserving hugepages requires root. Re-run with sudo.")
            sys.exit(errno.EPERM)

        try:
            actual = int(target.read_text())
            # count(0) is the documented way to release the pool, so reading
            # back 0 is success there rather than a failed reservation.
            if args.count and not actual:
                log.error(
                    f"No hugepages were reserved out of count({args.count}) for size({args.size}) kB"
                )
            elif actual < args.count:
                log.warning(
                    f"Only {actual} hugepage(s) were reserved out of count({args.count}) for size({args.size}) kB"
                )
        except Exception as exc:
            log.error(f"Failed to verify hugepage allocation: {exc}")
            sys.exit(1)

    def mount(self, args):
        mountpoint = Path(args.mountpoint or "/dev/hugepages")
        if not mountpoint.exists():
            mountpoint.mkdir(parents=True)

        cmd = ["mount", "-t", "hugetlbfs", "nodev", str(mountpoint)]
        if args.pagesize:
            cmd += ["-o", f"pagesize={args.pagesize}k"]
        result = run(cmd)
        if result.returncode != 0:
            log.error(f"Failed to mount hugetlbfs: {result.stderr}")
            sys.exit(1)
        print(f"Mounted hugetlbfs at {mountpoint}")


class FreeBSDBackend:
    """Large-page (superpage) inspection on FreeBSD.

    FreeBSD manages large pages transparently as reservation-based
    "superpages": there is no manually reserved pool and no hugetlbfs.
    The VM promotes and demotes superpages automatically, so ``setup`` and
    ``mount`` are informational on this platform and only ``info`` reports
    real state, sourced from sysctl.

    Not configurable: ``setup`` takes no --count and there are no --size
    choices to enumerate, so no ``supported_sizes()``.
    """

    configurable = False

    # amd64/i386 name the superpage knob vm.pmap.pg_ps_enabled and keep the
    # 2 MiB counters under vm.pmap.pde; arm64 uses vm.pmap.superpages_enabled
    # and vm.pmap.l2. Try each spelling and use whichever the kernel answers.
    ENABLED_OIDS = ("vm.pmap.pg_ps_enabled", "vm.pmap.superpages_enabled")
    STAT_NODES = ("vm.pmap.pde", "vm.pmap.l2")
    STAT_LEAVES = ("mappings", "promotions", "demotions", "p_failures")

    def _sysctl(self, name):
        result = run(["sysctl", "-n", name])
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    def _first_sysctl(self, names):
        """Return (name, value) for the first of `names` the kernel answers"""

        for name in names:
            value = self._sysctl(name)
            if value is not None:
                return name, value
        return None, None

    @staticmethod
    def parse_pagesizes(raw):
        """Parse hw.pagesizes, which sysctl(8) renders in two formats

        FreeBSD 13 and later print "{ 4096, 2097152 }"; older releases print a
        plain space-separated array padded with zeroes up to MAXPAGESIZES.
        Pull the integers out of either form and drop the zero padding.
        """

        if not raw:
            return []
        return [size for size in (int(tok) for tok in re.findall(r"\d+", raw)) if size]

    def _pagesizes(self):
        return self.parse_pagesizes(self._sysctl("hw.pagesizes"))

    def info(self, args):
        # Collected first so a run that reads nothing fails without having
        # already printed a header onto stdout.
        lines = []

        name, enabled = self._first_sysctl(self.ENABLED_OIDS)
        if enabled is not None:
            state = "enabled" if enabled == "1" else "disabled"
            lines.append(f"  Superpages: {state} ({name}={enabled})")

        for size in self._pagesizes():
            lines.append(f"  Page size: {size} bytes ({size // 1024} kB)")

        for node in self.STAT_NODES:
            stats = [(leaf, self._sysctl(f"{node}.{leaf}")) for leaf in self.STAT_LEAVES]
            stats = [(leaf, value) for leaf, value in stats if value is not None]
            if not stats:
                continue
            lines.append(f"  2 MiB superpage mappings ({node}):")
            lines += [f"    {leaf}: {value}" for leaf, value in stats]
            break

        if not lines:
            log.error("Could not read any superpage state from sysctl; nothing to report.")
            sys.exit(1)

        print("Hugepage (superpage) Support:")
        print("\n".join(lines))

    def setup(self, args):
        print(
            "FreeBSD manages large pages as transparent, reservation-based "
            "superpages.\n"
            "There is no manually reserved pool to configure; the VM promotes "
            "and\n"
            "demotes superpages automatically."
        )
        name, enabled = self._first_sysctl(self.ENABLED_OIDS)
        if enabled is not None:
            state = "enabled" if enabled == "1" else "disabled"
            print(f"Superpages are currently {state} ({name}={enabled}).")
            print(f"To disable superpages globally, set the loader tunable {name}=0.")

    def mount(self, args):
        print(
            "FreeBSD has no hugetlbfs to mount.\n"
            "Applications request superpages directly via "
            "mmap(..., MAP_ALIGNED_SUPER);\n"
            "the kernel backs the mapping with large pages when alignment and "
            "size permit."
        )


def get_backend(system=None):
    system = system or platform.system()
    if system == "Linux":
        return LinuxBackend()
    if system == "FreeBSD":
        return FreeBSDBackend()
    return None


def parse_args(backend):
    # Only ask backends that actually reserve pages. Probing a backend costs a
    # sysctl(8) call on FreeBSD, and it would run on every invocation --
    # including --help and --version -- to populate a --size choice that the
    # informational FreeBSD setup never reads.
    try:
        supported_sizes = backend.supported_sizes() if backend and backend.configurable else []
    except Exception as exc:
        supported_sizes = []
        log.warning(f"Could not read supported hugepage sizes: {exc}")

    parser = argparse.ArgumentParser(description="Inspect and manage Linux/FreeBSD hugepages")

    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")

    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("info", help="Show hugepage status and capabilities")

    setup = subparsers.add_parser("setup", help="Configure hugepage pool")

    setup.add_argument(
        "--size",
        choices=supported_sizes if supported_sizes else None,
        default=supported_sizes[0] if supported_sizes else None,
        help="Hugepage size in kB",
    )

    # Only backends that actually reserve pages need a count; on FreeBSD setup
    # is informational and must stay runnable with no arguments.
    setup.add_argument(
        "--count",
        required=bool(backend and backend.configurable),
        type=int,
        help="Number of pages to reserve",
    )

    mount = subparsers.add_parser("mount", help="Mount hugetlbfs")
    mount.add_argument("--mountpoint", help="Mount location (default: /dev/hugepages)")
    mount.add_argument("--pagesize", help="Optional hugepage size in kB")

    parser.add_argument(
        "--print-completion",
        choices=["bash"],
        metavar="SHELL",
        help="Print shell completion script to stdout and exit",
    )

    return parser.parse_args()


def main():
    backend = get_backend()
    args = parse_args(backend)

    if args.print_completion == "bash":
        sys.stdout.write(BASH_COMPLETION)
        return

    # force=True: probing the platform in parse_args() may already have logged,
    # which implicitly configures the root logger. Without force this call is a
    # no-op and --verbose silently does nothing.
    log.basicConfig(
        level=log.DEBUG if args.verbose else log.INFO,
        format="# %(levelname)s: %(message)s",
        force=True,
    )

    if args.command is None:
        log.error("No command specified. Use --help.")
        sys.exit(1)

    if backend is None:
        log.error(f"Unsupported platform: {platform.system()}. Supported: Linux, FreeBSD.")
        sys.exit(1)

    if args.command == "info":
        backend.info(args)
    elif args.command == "setup":
        backend.setup(args)
    elif args.command == "mount":
        backend.mount(args)


if __name__ == "__main__":
    main()
