#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) Simon Andreas Frimann Lund <os@safl.dk>
#
# Tool for inspecting and configuring hugepages on Linux and FreeBSD
#
# Platform-specific operations (reporting, reserving, and exposing large
# pages) are handled by a Platform selected at runtime via get_platform():
#
# * Linux   -- the hugepage pool in /sys/kernel/mm/hugepages + hugetlbfs
# * FreeBSD -- the DPDK contigmem module, whose buffers appear as
#              /dev/contigmem
#
# Kept as a single, stdlib-only file so it can be installed by copying this
# script.
#
import abc
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
from typing import Optional

__version__ = "0.2.10"

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

    log.debug(f"cmd({shlex.join(cmd)})")
    try:
        # LC_ALL=C: callers classify failures by matching English strerror text.
        return subprocess.run(
            cmd, capture_output=True, text=True, env={**os.environ, "LC_ALL": "C"}
        )
    except OSError as exc:
        # Shell convention for exec failures.
        code = 127 if isinstance(exc, FileNotFoundError) else 126
        return subprocess.CompletedProcess(cmd, code, stdout="", stderr=str(exc))


def read_int(path: Path) -> int:
    """Read a single integer out of a sysfs file"""

    try:
        return int(path.read_text())
    except (OSError, ValueError) as exc:
        raise OSError(errno.EIO, f"Failed to read {path}: {exc}") from exc


def sysfs_write(path: Path, text):
    log.debug(f'{path} "{text}"')
    with os.fdopen(os.open(path, os.O_WRONLY), "w") as f:
        return f.write(f"{text}\n")


class Platform(abc.ABC):
    """Platform-specific hugepage operations"""

    @abc.abstractmethod
    def info(self):
        """Print the current hugepage state"""

    @abc.abstractmethod
    def setup(self, size: Optional[int], count: int):
        """Reserve count pages of size kB

        A size of None picks the platform default. A count of 0 releases
        the pool.
        """

    @abc.abstractmethod
    def mount(self, mountpoint: Optional[str] = None, pagesize: Optional[str] = None):
        """Make the reserved pages reachable through the filesystem"""


# --- Linux platform -----------------------------------------------------------


class Linux(Platform):
    """Hugepage management on Linux

    Reads the per-size pools under /sys/kernel/mm/hugepages, reserves pages
    by writing nr_hugepages, and mounts hugetlbfs with mount(8).
    """

    #: Per-size hugepage pools exported by the kernel
    SYSFS = Path("/sys/kernel/mm/hugepages")

    def supported_sizes(self) -> list:
        sizes = []
        for entry in self.SYSFS.glob("hugepages-*kB"):
            token = entry.name.split("-")[1].replace("kB", "")
            if token.isdigit():
                sizes.append(int(token))
        return sizes

    def info(self):
        print("Hugepage Support:")
        for entry in self.SYSFS.glob("hugepages-*kB"):
            size = entry.name.split("-")[1]
            nr = read_int(entry / "nr_hugepages")
            free = read_int(entry / "free_hugepages")
            resv = read_int(entry / "resv_hugepages")
            print(f"  Size: {size}  Total: {nr}  Free: {free}  Reserved: {resv}")

    def setup(self, size: Optional[int], count: int):
        """Setup hugepages via sysfs"""

        if count < 0:
            raise OSError(
                errno.EINVAL, f"Invalid count: {count}. Give a page count, or 0 to release."
            )

        if size is None:
            supported = self.supported_sizes()
            if not supported:
                raise OSError(errno.ENOTSUP, "No hugepage sizes are supported by this kernel.")
            size = min(supported)

        target = self.SYSFS / f"hugepages-{size}kB" / "nr_hugepages"
        if not target.exists():
            supported = ", ".join(f"{each}" for each in self.supported_sizes())
            raise OSError(
                errno.EINVAL,
                f"Invalid hugepage size: {size} kB. This kernel supports: {supported or 'none'}.",
            )

        try:
            sysfs_write(target, str(count))
        except PermissionError as exc:
            raise PermissionError(
                errno.EPERM, "Reserving hugepages requires root. Re-run with sudo."
            ) from exc

        actual = read_int(target)

        # Writing 0 releases the pool, so a zero readback is success there.
        if not count:
            if actual:
                raise OSError(
                    errno.EBUSY,
                    f"{actual} hugepage(s) of size({size}) kB are still reserved; "
                    "pages in use cannot be released.",
                )
            return
        if not actual:
            raise OSError(
                errno.ENOMEM,
                f"No hugepages were reserved out of count({count}) for size({size}) kB",
            )
        if actual < count:
            log.warning(
                f"Only {actual} hugepage(s) were reserved out of count({count}) "
                f"for size({size}) kB"
            )

    def mount(self, mountpoint: Optional[str] = None, pagesize: Optional[str] = None):
        target = Path(mountpoint or "/dev/hugepages")
        try:
            target.mkdir(parents=True, exist_ok=True)
        except PermissionError as exc:
            raise PermissionError(
                errno.EPERM, f"Creating {target} requires root. Re-run with sudo."
            ) from exc
        except OSError as exc:
            raise OSError(
                exc.errno or errno.EIO, f"Failed to create {target}: {exc.strerror}"
            ) from exc

        cmd = ["mount", "-t", "hugetlbfs", "nodev", str(target)]
        if pagesize:
            cmd += ["-o", f"pagesize={pagesize}k"]
        result = run(cmd)
        if result.returncode != 0:
            raise OSError(
                errno.EIO,
                f"Failed to mount hugetlbfs (mount exited {result.returncode}): "
                f"{result.stderr.strip()}",
            )
        print(f"Mounted hugetlbfs at {target}")


# --- FreeBSD platform ---------------------------------------------------------


class FreeBSD(Platform):
    """Contiguous-memory pool management on FreeBSD via DPDK's contigmem

    FreeBSD has no hugetlbfs and no reservable hugepage pool. The DPDK
    contigmem kernel module fills that role: at load time it reserves
    physically contiguous buffers and exposes them at /dev/contigmem.
    ``setup`` maps size/count onto the module tunables and (re)loads it;
    ``info`` reads the pool back from the hw.contigmem sysctls.
    """

    #: contigmem's own default buffer size, in kB
    DEFAULT_SIZE = 524288

    #: Compile-time cap in the module (RTE_CONTIGMEM_MAX_NUM_BUFS)
    MAX_BUFFERS = 64

    #: Device node the reserved buffers are mapped through
    DEVICE = Path("/dev/contigmem")

    def _sysctl(self, name):
        """Return the value of one OID, or None when it cannot be read"""

        result = run(["sysctl", "-n", name])
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    def _loaded(self) -> bool:
        """Report whether contigmem is in the kernel"""

        result = run(["kldstat", "-q", "-m", "contigmem"])
        # kldstat itself failing says nothing about the module.
        if result.returncode in (126, 127):
            raise OSError(
                errno.ENOENT,
                f"Could not run kldstat: {result.stderr.strip()}\nCheck that /sbin is on PATH.",
            )
        return result.returncode == 0

    @staticmethod
    def _parse_pagesizes(raw):
        """Parse hw.pagesizes, which sysctl(8) renders in two formats

        FreeBSD 13 and later print "{ 4096, 2097152 }"; older releases
        print a plain space-separated array padded with zeroes. Pull the
        integers out of either form and drop the zero padding.
        """

        if not raw:
            return []
        return [size for size in (int(tok) for tok in re.findall(r"\d+", raw)) if size]

    def _pool(self):
        """Return (num_buffers, buffer_size) or None while unreadable"""

        num = self._sysctl("hw.contigmem.num_buffers")
        size = self._sysctl("hw.contigmem.buffer_size")
        try:
            return int(num), int(size)
        except (TypeError, ValueError):
            return None

    def _raise_if_denied(self, result, action):
        """Raise PermissionError with targeted advice when the kernel refused"""

        if "not permitted" not in result.stderr.lower():
            return
        # EPERM also hits root under securelevel >= 1 and in jails.
        if os.geteuid() != 0:
            advice = f"{action} requires root. Re-run with sudo."
        else:
            advice = "Refused even for root. Check kern.securelevel and jail restrictions."
        raise PermissionError(errno.EPERM, f"{result.stderr.strip()}\n{advice}")

    @staticmethod
    def _loader_conf(count, size_bytes):
        """The /boot/loader.conf lines that reserve the pool at boot"""

        return [
            f"hw.contigmem.num_buffers={count}",
            f"hw.contigmem.buffer_size={size_bytes}",
            'contigmem_load="YES"',
        ]

    def info(self):
        sizes = self._parse_pagesizes(self._sysctl("hw.pagesizes"))
        if sizes:
            print("Page sizes: " + ", ".join(f"{size // 1024} kB" for size in sizes))

        if not self._loaded():
            print("Contigmem pool: not loaded")
            print("Reserve one with: hugepages setup --size <kB> --count <buffers>")
            return

        pool = self._pool()
        if pool is None:
            raise OSError(errno.EIO, "contigmem is loaded but hw.contigmem is unreadable.")

        num, size = pool
        references = self._sysctl("hw.contigmem.num_references") or "0"
        print("Contigmem pool:")
        print(f"  Buffers: {num}  Size: {size // 1024} kB  Total: {num * size // 1024} kB")
        print(f"  References: {references}")
        for index in range(num):
            physaddr = self._sysctl(f"hw.contigmem.physaddr.{index}")
            try:
                print(f"  Buffer {index}: physical address {int(physaddr):#x}")
            except (TypeError, ValueError):
                log.warning(f"Could not read the address of buffer {index}.")

    def setup(self, size: Optional[int], count: int):
        """Reserve the pool by (re)loading contigmem with the requested tunables"""

        if not count:
            self._release()
            return

        size_kb = self.DEFAULT_SIZE if size is None else size

        if count < 0:
            raise OSError(
                errno.EINVAL, f"Invalid count: {count}. Give a buffer count, or 0 to release."
            )
        # The module hard-rejects both. Failing early keeps a load attempt
        # from unloading a working pool first.
        if count > self.MAX_BUFFERS:
            raise OSError(
                errno.EINVAL,
                f"count({count}) exceeds the module's cap of {self.MAX_BUFFERS} buffers.",
            )

        size_bytes = size_kb * 1024
        try:
            pagesize = int(self._sysctl("hw.pagesize"))
        except (TypeError, ValueError):
            pagesize = 4096
        if size_bytes < pagesize or size_bytes & (size_bytes - 1):
            raise OSError(
                errno.EINVAL,
                f"Invalid buffer size: {size_kb} kB. "
                f"contigmem needs a power-of-two size of at least {pagesize // 1024} kB.",
            )

        released = ""
        if self._loaded():
            pool = self._pool()
            if pool is None:
                # Unloading a pool we cannot compare would be destructive.
                raise OSError(
                    errno.EIO,
                    "contigmem is loaded but hw.contigmem is unreadable; not touching it.",
                )
            if pool == (count, size_bytes):
                print(f"contigmem already provides {count} x {size_kb} kB buffer(s).")
                return
            # Tunables only apply at load time, so reconfiguring means a reload.
            self._unload()
            released = f"{pool[0]} x {pool[1] // 1024} kB buffer(s)"

        try:
            self._load(count, size_bytes)
        except OSError as exc:
            # Past the unload the machine has no pool at all, whatever went
            # wrong afterward.
            if released:
                exc.strerror = (
                    f"The previous pool of {released} was released and is gone.\n{exc.strerror}"
                )
            raise

        total_kb = count * size_bytes // 1024
        print(f"Reserved {count} x {size_kb} kB buffer(s) ({total_kb} kB total) at {self.DEVICE}")
        print("To reserve at every boot, add to /boot/loader.conf:")
        for line in self._loader_conf(count, size_bytes):
            print(f"  {line}")

    def _load(self, count, size_bytes):
        """Set the tunables and load the module"""

        for name, value in (
            ("hw.contigmem.num_buffers", count),
            ("hw.contigmem.buffer_size", size_bytes),
        ):
            result = run(["kenv", f"{name}={value}"])
            if result.returncode != 0:
                self._raise_if_denied(result, f"Setting {name}")
                raise OSError(errno.EIO, f"Failed to set {name}: {result.stderr.strip()}")

        result = run(["kldload", "contigmem"])
        if result.returncode != 0:
            stderr = result.stderr.strip()
            self._raise_if_denied(result, "Loading contigmem")
            lines = [f"Failed to load contigmem: {stderr}"]
            # A missing binary also matches the module-missing text below.
            if result.returncode in (126, 127):
                lines.append("kldload(8) could not be run. Check that /sbin is on PATH.")
            elif "no such file" in stderr.lower():
                lines.append("contigmem.ko ships with DPDK. Install it with: pkg install dpdk")
            elif "invalid argument" in stderr.lower():
                # Reserving at boot would fail the same way.
                lines.append("The module rejected this configuration. Adjust --size/--count.")
            else:
                lines.append("If memory is too fragmented, reserve at boot via /boot/loader.conf:")
                lines += [f"  {line}" for line in self._loader_conf(count, size_bytes)]
            raise OSError(errno.EIO, "\n".join(lines))

        actual = self._pool()
        if actual != (count, size_bytes):
            raise OSError(
                errno.EIO, f"contigmem loaded, but the pool is {actual}, not the requested one."
            )

    def _release(self):
        if not self._loaded():
            print("contigmem is not loaded; nothing to release.")
            return
        self._unload()
        print("Unloaded contigmem and released its buffers.")

    def _unload(self):
        result = run(["kldunload", "contigmem"])
        if result.returncode == 0:
            return
        if result.returncode in (126, 127):
            raise OSError(
                errno.ENOENT,
                f"Could not run kldunload: {result.stderr.strip()}\nCheck that /sbin is on PATH.",
            )
        self._raise_if_denied(result, "Unloading contigmem")
        message = f"Failed to unload contigmem: {result.stderr.strip()}"
        references = self._sysctl("hw.contigmem.num_references")
        if references and references != "0":
            message += (
                f"\n{references} mapping(s) still reference the pool. "
                "Stop the processes using it first."
            )
        raise OSError(errno.EBUSY, message)

    def mount(self, mountpoint: Optional[str] = None, pagesize: Optional[str] = None):
        if mountpoint or pagesize:
            log.warning("--mountpoint and --pagesize are Linux-only and are ignored here.")

        if not self.DEVICE.exists():
            raise OSError(
                errno.ENOENT,
                f"{self.DEVICE} does not exist. Reserve a pool first: hugepages setup",
            )
        print("FreeBSD has no hugetlbfs to mount.")
        print(f"The contigmem pool is available at {self.DEVICE}.")
        print("Map buffer i with mmap on that device at offset i * PAGE_SIZE.")


def get_platform() -> Platform:
    """Return the Platform implementation for the running system"""

    system = platform.system()
    if system == "Linux":
        return Linux()
    if system == "FreeBSD":
        return FreeBSD()

    raise NotImplementedError(
        f"hugepages does not support platform '{system}'; supported: Linux, FreeBSD"
    )


def parse_args():
    parser = argparse.ArgumentParser(
        description="Inspect and manage hugepages on Linux and FreeBSD"
    )

    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")

    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("info", help="Show hugepage status and capabilities")

    setup = subparsers.add_parser("setup", help="Configure hugepage pool")

    # The choices are platform-specific, so they are validated by the
    # platform rather than baked into the parser.
    setup.add_argument(
        "--size",
        type=int,
        help="Hugepage size in kB (FreeBSD: contigmem buffer size, default 512 MiB)",
    )

    setup.add_argument(
        "--count", required=True, type=int, help="Number of pages (FreeBSD: buffers) to reserve"
    )

    mount = subparsers.add_parser(
        "mount", help="Mount hugetlbfs (FreeBSD: report the pool device)"
    )
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
    args = parse_args()

    if args.print_completion == "bash":
        sys.stdout.write(BASH_COMPLETION)
        return

    # basicConfig() installs a handler only when there is none, so the level
    # has to be set separately for it to be authoritative.
    log.basicConfig(format="# %(levelname)s: %(message)s")
    log.getLogger().setLevel(log.DEBUG if args.verbose else log.INFO)

    if args.command is None:
        log.error("No command specified. Use --help.")
        sys.exit(1)

    try:
        plat = get_platform()
    except NotImplementedError as exc:
        log.error(str(exc))
        sys.exit(errno.ENOSYS)

    try:
        if args.command == "info":
            plat.info()
        elif args.command == "setup":
            plat.setup(args.size, args.count)
        elif args.command == "mount":
            plat.mount(args.mountpoint, args.pagesize)
    except OSError as exc:
        message = exc.strerror or str(exc)
        if exc.filename:
            message = f"{message}: {exc.filename}"
        for line in message.splitlines():
            log.error(line)
        sys.exit(exc.errno or 1)


if __name__ == "__main__":
    main()
