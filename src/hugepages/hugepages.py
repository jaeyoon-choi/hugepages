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
    # LC_ALL=C: error handling matches English strerror text in stderr.
    env = {**os.environ, "LC_ALL": "C"}
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


def sysfs_write(path: Path, text):
    log.info(f'{path} "{text}"')
    with os.fdopen(os.open(path, os.O_WRONLY), "w") as f:
        return f.write(f"{text}\n")


class LinuxBackend:
    """Hugepage management on Linux via sysfs and hugetlbfs

    A backend provides ``info``/``setup``/``mount``. ``setup`` reserves
    --count pages and releases them at --count 0. ``supported_sizes()``
    enumerates the --size choices. A backend with no fixed size list
    returns [] and supplies a free-form ``default_size`` instead. See
    parse_args().
    """

    # --size defaults to the first supported size.
    default_size = None

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

        if args.size is None:
            log.error(
                "No hugepage sizes under /sys/kernel/mm/hugepages. "
                "The kernel has no hugepage support."
            )
            sys.exit(1)

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
    """Contiguous DMA memory on FreeBSD via DPDK's contigmem module.

    FreeBSD has no hugetlbfs and no reserved hugepage pool. Pinned,
    physically contiguous memory comes from the contigmem kernel module
    that ships with DPDK. ``setup`` maps --count/--size onto the module's
    num_buffers/buffer_size tunables and (re)loads it. ``info`` reads the
    read-only hw.contigmem sysctls. ``mount`` stays informational because
    buffers are mmap'ed from /dev/contigmem, not a filesystem.

    contigmem takes any power-of-2 buffer size, so there is no fixed list
    for ``supported_sizes()`` to enumerate. --size stays free-form and
    defaults to ``default_size``.
    """

    # contigmem's own default buffer size (512 MB), in kB to match --size.
    default_size = str(512 * 1024)

    TUNABLE_COUNT = "hw.contigmem.num_buffers"
    TUNABLE_SIZE = "hw.contigmem.buffer_size"

    def supported_sizes(self):
        return []

    def _sysctl(self, name):
        result = run(["sysctl", "-n", name])
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    @staticmethod
    def parse_pagesizes(raw):
        """Parse hw.pagesizes, which sysctl(8) renders in two formats

        FreeBSD 13 and later print "{ 4096, 2097152 }". Older releases print a
        plain space-separated array padded with zeroes up to MAXPAGESIZES.
        Pull the integers out of either form and drop the zero padding.
        """

        if not raw:
            return []
        return [size for size in (int(tok) for tok in re.findall(r"\d+", raw)) if size]

    def _pagesizes(self):
        return self.parse_pagesizes(self._sysctl("hw.pagesizes"))

    @staticmethod
    def _large_pages(pagesizes):
        """Return the large page sizes, dropping the base page"""

        return sorted(size for size in pagesizes if size > min(pagesizes, default=0))

    @staticmethod
    def _human_size(size):
        for unit, div in (("GiB", 1024**3), ("MiB", 1024**2), ("kB", 1024)):
            if size >= div and size % div == 0:
                return f"{size // div} {unit}"
        return f"{size} bytes"

    def _alignment_note(self, addr, pagesizes):
        # A large page can only map memory aligned to its own size, so the
        # largest page size that divides the address is the best the kernel
        # can do for this buffer.
        for size in reversed(self._large_pages(pagesizes)):
            if addr % size == 0:
                return f" ({self._human_size(size)} aligned)"
        return ""

    def _loaded(self):
        # kldstat -q -m exits 0 only when the module is loaded.
        return run(["kldstat", "-q", "-m", "contigmem"]).returncode == 0

    def _fail(self, message, code=1):
        log.error(message)
        sys.exit(code)

    def _fail_if_not_permitted(self, result, action):
        if "not permitted" in result.stderr.lower():
            self._fail(f"{action} requires root. Re-run with sudo.", errno.EPERM)

    def _require_root(self, action):
        # kenv(1) omits the errno text on failure, so detect EPERM up front.
        if os.geteuid() != 0:
            self._fail(f"{action} requires root. Re-run with sudo.", errno.EPERM)

    def _kenv(self, name, value):
        result = run(["kenv", f"{name}={value}"])
        if result.returncode != 0:
            self._fail_if_not_permitted(result, "Setting contigmem tunables")
            self._fail(f"Failed to set {name}: {result.stderr.strip()}")

    def _kldunload(self):
        result = run(["kldunload", "contigmem"])
        if result.returncode == 0:
            return
        self._fail_if_not_permitted(result, "Unloading contigmem")
        if "busy" in result.stderr.lower():
            self._fail(
                "contigmem is busy: a running process still has its buffers mapped.\n"
                "Run 'fstat /dev/contigmem' to find it, stop it, and retry."
            )
        self._fail(f"Failed to unload contigmem: {result.stderr.strip()}")

    def _kldload(self, after_unload=False):
        result = run(["kldload", "contigmem"])
        if result.returncode == 0:
            return
        self._fail_if_not_permitted(result, "Loading contigmem")
        if "no such file" in result.stderr.lower():
            self._fail(
                "contigmem kernel module not found. It ships with DPDK as "
                "/boot/modules/contigmem.ko. The package name carries the "
                "DPDK version, e.g. pkg install dpdk25.11 (pkg search dpdk)."
            )
        released = "The previous reservation is already released.\n" if after_unload else ""
        self._fail(
            f"Failed to load contigmem: {result.stderr.strip()}\n{released}"
            "The kernel may lack enough free contiguous memory. Try a smaller "
            "--size or --count, or set the tunables in /boot/loader.conf so "
            "the buffers are allocated at boot."
        )

    def info(self, args):
        # Collected first so a run that reads nothing fails without having
        # already printed a header onto stdout.
        lines = []
        pagesizes = self._pagesizes()
        for size in pagesizes:
            lines.append(f"  Page size: {size} bytes ({size // 1024} kB)")

        if not self._loaded():
            lines.append("  contigmem: not loaded")
            lines.append("  Load it with: hugepages setup --count <n> [--size <kB>]")
            lines.append("  The module ships with DPDK (pkg search dpdk).")
        else:
            try:
                count = int(self._sysctl(self.TUNABLE_COUNT))
                size = int(self._sysctl(self.TUNABLE_SIZE))
            except (TypeError, ValueError):
                self._fail("contigmem is loaded but hw.contigmem is unreadable; no report.")
            lines.append("  contigmem: loaded")
            lines.append(f"  Buffers: {count} x {size} bytes ({size // 1024} kB)")
            refs = self._sysctl("hw.contigmem.num_references")
            if refs is not None:
                lines.append(f"  Mapped references: {refs}")
            for index in range(count):
                physaddr = self._sysctl(f"hw.contigmem.physaddr.{index}")
                try:
                    addr = int(physaddr, 0)
                except (TypeError, ValueError):
                    continue
                note = self._alignment_note(addr, pagesizes)
                lines.append(f"  Buffer {index}: physaddr 0x{addr:x}{note}")

        print("Hugepage (contigmem) Support:")
        print("\n".join(lines))

    def setup(self, args):
        """Reserve contigmem buffers by (re)loading the kernel module"""

        if args.count == 0:
            if not self._loaded():
                print("contigmem is not loaded; nothing to release.")
                return
            self._require_root("Releasing contigmem buffers")
            self._kldunload()
            print("Released contigmem buffers (module unloaded).")
            return
        if args.count < 0:
            self._fail(f"Invalid count: {args.count}")

        try:
            size_kb = int(args.size)
        except (TypeError, ValueError):
            self._fail(f"Invalid buffer size: {args.size}")
        size_bytes = size_kb * 1024
        # contigmem rejects sizes that are not a power of 2 at load time.
        if size_bytes <= 0 or size_bytes & (size_bytes - 1):
            self._fail(f"Invalid buffer size: {size_kb} kB. contigmem needs a power of 2.")

        self._require_root("Reserving contigmem buffers")

        large_pages = self._large_pages(self._pagesizes())
        if large_pages and size_bytes < large_pages[0]:
            log.warning(
                f"{size_kb} kB is smaller than the smallest large page "
                f"({large_pages[0] // 1024} kB). "
                "The kernel cannot map this buffer with one."
            )

        # Tunables are read only at load time, so resizing needs a reload.
        was_loaded = self._loaded()
        if was_loaded:
            self._kldunload()
        self._kenv(self.TUNABLE_COUNT, str(args.count))
        self._kenv(self.TUNABLE_SIZE, str(size_bytes))
        self._kldload(after_unload=was_loaded)

        actual = self._sysctl(self.TUNABLE_COUNT)
        if actual is None:
            self._fail("contigmem loaded but hw.contigmem is unreadable; cannot verify.")
        print(f"Reserved {actual} x {size_kb} kB contigmem buffer(s).")
        print("To keep this across reboots, add to /boot/loader.conf:")
        print(f"  {self.TUNABLE_COUNT}={args.count}")
        print(f"  {self.TUNABLE_SIZE}={size_bytes}")
        print('  contigmem_load="YES"')

    def mount(self, args):
        print(
            "FreeBSD has no hugetlbfs to mount.\n"
            "Pinned DMA memory comes from the contigmem module instead:\n"
            "applications mmap(2) /dev/contigmem, where buffer i sits at\n"
            "offset i * hw.contigmem.buffer_size."
        )


def get_backend(system=None):
    system = system or platform.system()
    if system == "Linux":
        return LinuxBackend()
    if system == "FreeBSD":
        return FreeBSDBackend()
    return None


def parse_args(backend):
    # A backend with no fixed size list returns [] and supplies default_size.
    try:
        supported_sizes = backend.supported_sizes() if backend else []
    except Exception as exc:
        supported_sizes = []
        log.warning(f"Could not read supported hugepage sizes: {exc}")

    if supported_sizes:
        default_size = supported_sizes[0]
    else:
        default_size = backend.default_size if backend else None

    parser = argparse.ArgumentParser(description="Inspect and manage Linux/FreeBSD hugepages")

    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")

    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("info", help="Show hugepage status and capabilities")

    setup = subparsers.add_parser("setup", help="Configure hugepage pool")

    setup.add_argument(
        "--size",
        choices=supported_sizes if supported_sizes else None,
        default=default_size,
        help="Hugepage size in kB",
    )

    # Unsupported platforms keep --count optional so --help still works.
    # main() rejects them before setup runs.
    setup.add_argument(
        "--count",
        required=backend is not None,
        type=int,
        help="Number of pages/buffers to reserve (0 releases them)",
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
