![hugepages: inspect and manage Linux hugepages](https://raw.githubusercontent.com/xnvme/hugepages/main/assets/banner.svg)

# hugepages

[![PyPI](https://img.shields.io/pypi/v/hugepages.svg)](https://pypi.org/project/hugepages/)
[![Python](https://img.shields.io/pypi/pyversions/hugepages.svg)](https://pypi.org/project/hugepages/)
[![Test](https://github.com/xnvme/hugepages/actions/workflows/test.yml/badge.svg)](https://github.com/xnvme/hugepages/actions/workflows/test.yml)

`hugepages` is a small CLI for inspecting and configuring hugepages on
Linux and FreeBSD. On Linux it reports current totals, free, and reserved
counts per supported page size, reserves pages via the sysfs interface at
`/sys/kernel/mm/hugepages/`, and mounts the hugetlbfs filesystem
(default `/dev/hugepages`). On FreeBSD it manages the pool of physically
contiguous buffers provided by the DPDK `contigmem` kernel module; see
[Platform differences](#platform-differences).

## Install

```
pipx install hugepages
```

Or standalone (single-file, stdlib only, no pip needed):

```
curl -fsSL https://raw.githubusercontent.com/xnvme/hugepages/main/src/hugepages/hugepages.py \
  -o ~/.local/bin/hugepages && chmod +x ~/.local/bin/hugepages
```

## Shell completion

```
hugepages --print-completion bash > ~/.local/share/bash-completion/completions/hugepages
```

Open a new shell (or `source` the file) and tab-completion is live: `hugepages <TAB>` lists `info setup mount`.

## Usage

```
$ hugepages --help
usage: hugepages [-h] [--version] [--verbose] [--print-completion SHELL]
                 {info,setup,mount} ...

Inspect and manage Linux/FreeBSD hugepages

positional arguments:
  {info,setup,mount}
    info                Show hugepage status and capabilities
    setup               Configure hugepage pool
    mount               Mount hugetlbfs

options:
  -h, --help            show this help message and exit
  --version             show program's version number and exit
  --verbose             Enable verbose logging
  --print-completion SHELL
                        Print shell completion script to stdout and exit
```

A few common invocations:

```
hugepages info                                  # current pool state + supported sizes
sudo hugepages setup --count 512                # reserve 512 pages at the smallest supported size
sudo hugepages setup --size 2048 --count 1024   # reserve 1024 x 2 MiB (explicit size)
sudo hugepages mount                            # mount hugetlbfs at /dev/hugepages
```

1 GiB hugepages are only available if the kernel was booted with
`default_hugepagesz=1G hugepagesz=1G hugepages=N` on the cmdline; the
kernel reserves the 1 GiB pool at boot, and `hugepages setup` cannot
enable that size after the fact. `hugepages info` lists the sizes the
running kernel actually supports.

`hugepages info` sample output (pool not yet reserved):

```
Hugepage Support:
  Size: 2048kB  Total: 0  Free: 0  Reserved: 0
  Size: 1048576kB  Total: 0  Free: 0  Reserved: 0
```

## Allocation paths

`hugepages setup` reserves pages in the kernel pool. Programs that
allocate via `memfd_create(..., MFD_HUGETLB)` or
`mmap(..., MAP_HUGETLB)` draw directly from the pool; no filesystem is
needed. Modern DPDK and custom xNVMe/uPCIe code take this path.

`hugepages mount` additionally mounts the `hugetlbfs` pseudo-filesystem
(default `/dev/hugepages`). Programs that want file-backed hugepages
with named-page semantics open and mmap files under the mountpoint.
SPDK and classic DPDK with `--huge-dir` use this path.

## Platform differences

Linux and FreeBSD expose large pages through different models, so the
same subcommands map to different mechanisms per platform:

| Command | Linux | FreeBSD |
| ------- | ----- | ------- |
| `info`  | totals/free/reserved per size from `/sys/kernel/mm/hugepages/` | contigmem pool state from the `hw.contigmem` sysctls |
| `setup` | reserves pages in the kernel pool via sysfs | sets the module tunables and (re)loads `contigmem.ko` |
| `mount` | mounts `hugetlbfs` (default `/dev/hugepages`) | informational — the pool is a device node, `/dev/contigmem` |

FreeBSD has no hugetlbfs and no reservable hugepage pool. The DPDK
`contigmem` kernel module fills that role: at load time it reserves
`hw.contigmem.num_buffers` physically contiguous buffers of
`hw.contigmem.buffer_size` bytes each and exposes them at
`/dev/contigmem`. `hugepages setup --size <kB> --count <n>` maps `--size`
onto the buffer size and `--count` onto the number of buffers, then
(re)loads the module. Sizes must be powers of two; the default is
contigmem's own 512 MiB. `--count 0` unloads the module and releases the
memory. Applications map buffer `i` by calling `mmap` on
`/dev/contigmem` with offset `i * PAGE_SIZE`.

The module ships with DPDK (`pkg install dpdk`). Its tunables only apply
at load time, so reconfiguring reloads the module — that fails while a
process still maps the pool. Physically contiguous memory fragments as
the system runs, so large pools are best reserved at boot; `setup`
prints the `/boot/loader.conf` lines that do so.

## Related

- [`devbind`](https://github.com/xnvme/devbind): inspect and control PCI device-driver binding in Linux.
- [`iommu`](https://github.com/safl/iommu): inspect and configure the IOMMU in Linux.
