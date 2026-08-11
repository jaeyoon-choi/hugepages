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

    A backend provides ``supported_sizes``/``info``/``setup``/``mount``;
    get_backend() picks one per platform and main() dispatches to it.
    ``supported_sizes`` lists the --size choices; an empty list means
    --size is free-form and ``setup`` validates it, with ``default_size``
    as the fallback default.
    """

    default_size = None  # --size defaults to the first supported size

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
    """Contiguous-memory pool management on FreeBSD via DPDK's contigmem

    FreeBSD has no hugetlbfs and no reservable hugepage pool. The DPDK
    contigmem kernel module fills that role: at load time it reserves
    physically contiguous buffers and exposes them at /dev/contigmem.
    ``setup`` maps --size/--count onto the module tunables and (re)loads
    it; ``info`` reads the pool back from the hw.contigmem sysctls.
    """

    # contigmem's own default buffer size: 512 MiB, expressed in kB.
    default_size = "524288"

    # Compile-time cap in the module (RTE_CONTIGMEM_MAX_NUM_BUFS).
    MAX_BUFFERS = 64

    DEVICE = Path("/dev/contigmem")

    def supported_sizes(self):
        # Any power-of-two size loads, so there are no fixed choices;
        # setup() validates instead.
        return []

    def _sysctl(self, name):
        result = run(["sysctl", "-n", name])
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    def _pool(self):
        """Return (num_buffers, buffer_size) or None while not loaded"""

        num = self._sysctl("hw.contigmem.num_buffers")
        size = self._sysctl("hw.contigmem.buffer_size")
        if num is None or size is None:
            return None
        return int(num), int(size)

    @staticmethod
    def loader_conf(count, size_bytes):
        """The /boot/loader.conf lines that reserve the pool at boot"""

        return [
            f"hw.contigmem.num_buffers={count}",
            f"hw.contigmem.buffer_size={size_bytes}",
            'contigmem_load="YES"',
        ]

    def info(self, args):
        pool = self._pool()
        if pool is None:
            print("Contigmem pool: not loaded")
            print("Reserve one with: hugepages setup --size <kB> --count <buffers>")
            return

        num, size = pool
        references = self._sysctl("hw.contigmem.num_references") or "0"
        print("Contigmem pool:")
        print(f"  Buffers: {num}  Size: {size // 1024} kB  Total: {num * size // 1024} kB")
        print(f"  References: {references}")
        for index in range(num):
            physaddr = self._sysctl(f"hw.contigmem.physaddr.{index}")
            if physaddr is not None:
                print(f"  Buffer {index}: physical address {int(physaddr):#x}")

    def setup(self, args):
        """Reserve the pool by (re)loading contigmem with the requested tunables"""

        if os.geteuid() != 0:
            log.error("Configuring contigmem requires root. Re-run with sudo.")
            sys.exit(errno.EPERM)

        if not args.count:
            self._release()
            return

        try:
            size_kb = int(args.size)
        except (TypeError, ValueError):
            log.error(f"Invalid buffer size: {args.size!r}. Give the size in kB.")
            sys.exit(1)

        size_bytes = size_kb * 1024
        pagesize = int(self._sysctl("hw.pagesize") or 4096)
        # The module rejects sizes that are not powers of two above the page size.
        if args.count < 0 or size_bytes <= pagesize or size_bytes & (size_bytes - 1):
            log.error(
                f"Invalid pool: count({args.count}) x size({size_kb}) kB. "
                f"contigmem needs a power-of-two size larger than {pagesize // 1024} kB."
            )
            sys.exit(1)
        if args.count > self.MAX_BUFFERS:
            log.warning(
                f"count({args.count}) exceeds the module's default cap of "
                f"{self.MAX_BUFFERS} buffers; loading may fail."
            )

        pool = self._pool()
        if pool == (args.count, size_bytes):
            print(f"contigmem already provides {args.count} x {size_kb} kB buffer(s).")
            return
        if pool is not None:
            # Tunables only apply at load time, so reconfiguring means a reload.
            self._unload()

        for name, value in (
            ("hw.contigmem.num_buffers", args.count),
            ("hw.contigmem.buffer_size", size_bytes),
        ):
            result = run(["kenv", f"{name}={value}"])
            if result.returncode != 0:
                log.error(f"Failed to set {name}: {result.stderr.strip()}")
                sys.exit(1)

        result = run(["kldload", "contigmem"])
        if result.returncode != 0:
            stderr = result.stderr.strip()
            log.error(f"Failed to load contigmem: {stderr}")
            if "no such file" in stderr.lower():
                log.error("contigmem.ko ships with DPDK. Install it with: pkg install dpdk")
            else:
                log.error("If memory is too fragmented, reserve at boot via /boot/loader.conf:")
                for line in self.loader_conf(args.count, size_bytes):
                    log.error(f"  {line}")
            sys.exit(1)

        actual = self._pool()
        if actual != (args.count, size_bytes):
            log.error(f"contigmem loaded, but the pool is {actual}, not the requested one.")
            sys.exit(1)

        total_kb = args.count * size_bytes // 1024
        print(
            f"Reserved {args.count} x {size_kb} kB buffer(s) ({total_kb} kB total) at {self.DEVICE}"
        )
        print("To reserve at every boot, add to /boot/loader.conf:")
        for line in self.loader_conf(args.count, size_bytes):
            print(f"  {line}")

    def _release(self):
        if self._pool() is None:
            print("contigmem is not loaded; nothing to release.")
            return
        self._unload()
        print("Unloaded contigmem and released its buffers.")

    def _unload(self):
        result = run(["kldunload", "contigmem"])
        if result.returncode != 0:
            references = self._sysctl("hw.contigmem.num_references")
            log.error(f"Failed to unload contigmem: {result.stderr.strip()}")
            if references and references != "0":
                log.error(
                    f"{references} mapping(s) still reference the pool. "
                    "Stop the processes using it first."
                )
            sys.exit(1)

    def mount(self, args):
        print("FreeBSD has no hugetlbfs to mount.")
        if self.DEVICE.exists():
            print(f"The contigmem pool is available at {self.DEVICE}.")
            print("Map buffer i with mmap on that device at offset i * PAGE_SIZE.")
        else:
            print(f"{self.DEVICE} does not exist. Reserve a pool first: hugepages setup")


def get_backend(system=None):
    system = system or platform.system()
    if system == "Linux":
        return LinuxBackend()
    if system == "FreeBSD":
        return FreeBSDBackend()
    return None


def parse_args(backend):
    try:
        supported_sizes = backend.supported_sizes() if backend else []
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
        default=supported_sizes[0]
        if supported_sizes
        else (backend.default_size if backend else None),
        help="Hugepage size in kB (FreeBSD: contigmem buffer size in kB)",
    )

    setup.add_argument(
        "--count", required=True, type=int, help="Number of pages (FreeBSD: buffers) to reserve"
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

    # force=True: anything logged before this call (e.g. a parse_args()
    # warning) implicitly configures the root logger. Without force this
    # call is a no-op and --verbose silently does nothing.
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
