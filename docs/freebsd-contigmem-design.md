# Design: a FreeBSD platform backed by contigmem

`hugepages` reserves and reports large pages. On Linux that is the
kernel hugepage pool. FreeBSD has no equivalent, so this platform
drives the DPDK `contigmem` module instead, which gives FreeBSD a pool
that `setup` can really configure.

## Background

The Linux platform manages a real kernel object: the hugepage pool under
`/sys/kernel/mm/hugepages/`. FreeBSD has no such pool and no hugetlbfs;
its superpages are transparent and cannot be reserved.

The DPDK `contigmem` kernel module fills that gap. At load time it
reserves physically contiguous buffers and exposes them through the
`/dev/contigmem` device. Its contract, taken from the driver source
(`kernel/freebsd/contigmem/contigmem.c`, DPDK main branch, read on
2026-08-11 at
<https://github.com/DPDK/dpdk/blob/main/kernel/freebsd/contigmem/contigmem.c>):

- Tunables, read once at load time: `hw.contigmem.num_buffers`
  (default 1) and `hw.contigmem.buffer_size` (bytes, default 512 MiB).
  A count past 64 (`RTE_CONTIGMEM_MAX_NUM_BUFS`) fails the load with
  `EINVAL`.
- Read-only sysctls while loaded: `num_buffers`, `buffer_size`,
  `num_references`, and `physaddr.<i>` per buffer.
- The buffer size must be a power of two of at least `PAGE_SIZE`:
  `contigmem_load()` rejects `contigmem_buffer_size < PAGE_SIZE`.
- Applications map buffer `i` by calling `mmap` on `/dev/contigmem`
  with offset `i * PAGE_SIZE`: `contigmem_mmap_single()` decodes
  `buffer_index = *offset / PAGE_SIZE`.
- `kldunload` fails with `EBUSY` while mappings reference the pool.
- Tunables apply only at load time. Reconfiguring means unload + load.

## Command mapping

`--size` x `--count` = total reserved memory holds on both platforms:

| Command | Linux | FreeBSD |
| ------- | ----- | ------- |
| `setup --size S --count N` | reserve N pages of S kB via sysfs | load contigmem with N buffers of S kB |
| `setup --count 0` | release the pool | unload the module (no-op when not loaded) |
| `info` | totals/free/reserved per size | pool state from the `hw.contigmem` sysctls |
| `mount` | mount hugetlbfs | informational; the pool is a device node |

## setup flow (FreeBSD)

1. No up-front root gate. Each kenv/kld failure is classified from
   its stderr: "not permitted" exits `EPERM`, the same shape as the
   Linux `PermissionError` path. The advice depends on the effective
   user -- non-root gets the sudo hint, root is pointed at
   `kern.securelevel` and jails -- and the raw stderr is always
   shown. run() pins `LC_ALL=C` so localized strerror text cannot
   break the match. A release with nothing loaded needs no root.
2. `--count 0`: unload if loaded; success without work otherwise.
   `EBUSY` reports how many mappings still reference the pool.
3. Validate locally what the driver validates: the size must be a
   power of two of at least the page size, and the count at most 64.
   Both are hard errors. Letting the module reject an over-cap count
   would first unload a working pool, then fail the load with
   `EINVAL`, losing the pool for nothing.
4. Loaded state comes from `kldstat -q -m contigmem`, not from
   whether the sysctls read back. Already loaded: same configuration
   is a no-op; a different one unloads first, since tunables only
   apply at load time. A loaded pool whose sysctls do not read back
   stops setup with an error, the same way `info` reports it;
   unloading a pool we cannot compare against the request would be
   destructive.
5. Set both tunables with `kenv`, then `kldload contigmem`. A failure
   after a reload says the previous pool is gone, since the machine is
   left without one. The message shows stderr plus a targeted hint:
   name the binary when it could not be run, install DPDK when the
   module file is missing, adjust the request when the module rejects
   it with `EINVAL` (a boot-time reservation would fail identically),
   or the `/boot/loader.conf` lines when memory is likely too
   fragmented to allocate at runtime.
6. Read the pool back from sysctl and compare with the request. The
   load is all-or-nothing, so a mismatch is an error, not a partial
   reservation warning like on Linux.
7. Print a summary and the `/boot/loader.conf` lines for persistence.

## Interface changes

- `Platform` is an ABC whose methods take plain values, not the argparse
  namespace, so the CLI stays out of the platform contract.
- The parser is platform-neutral. `--size` has no choices and no
  default; each platform resolves `None` to its own default and
  validates what it is given. Fixed choices would be artificial for
  contigmem, which takes any power of two.
- Platform code raises `OSError` with an errno. `main()` catches it,
  prints one line per message line, and exits with that errno.
  `get_platform()` raises `NotImplementedError` on an unsupported
  system, which becomes `ENOSYS`.
- No `configurable` flag: both platforms reserve a real pool, so
  `--count` stays required everywhere.

## Decisions

- The platform reports the contigmem pool, not the transparent
  superpages the VM promotes on its own: those cannot be reserved, so
  they say nothing about what `setup` can do. `info` keeps one line of
  context, the `hw.pagesizes` list, parsed from both sysctl renderings
  (braced on 13+, zero-padded before).
- The tool never writes `/boot/loader.conf`; it prints the lines. A
  `--persist` flag can be added later if wanted.
- `mount` stays informational and state-aware: there is nothing to
  mount, so it points at `/dev/contigmem` and how to map it.
- Root is inferred from command stderr, not checked up front, so
  unprivileged runs still get the read-only and no-op paths. The
  effective uid only picks the advice: sudo for non-root, a
  securelevel/jail pointer for root.

## Testing

Tests live in `test_linux.py` and `test_freebsd.py`, with `test_cli.py`
kept for the shared entry points. Both platform suites run anywhere.

The FreeBSD suite drives a stateful fake
(`_FakeFreeBSD`) that simulates the driver contract: kenv values apply
only at kldload time, the `hw.contigmem` sysctls exist only while
loaded, kldunload fails while references remain, and a load past the
64-buffer cap fails like the kernel's `EINVAL`. The fake can also
drop privileges, hide binaries, and break the sysctls to drive the
error paths. This lets the order-sensitive flows -- reconfigure-by-
reload, readback verification, busy unload -- run against the same
rules the kernel enforces.

## Known limitations

`info` runs one `sysctl` per buffer, so a full 64-buffer pool costs 64
reads. `sysctl(8)` accepts several names at once; batching them is left
for later.

A failed `kldload` leaves the `kenv` tunables set for the rest of the
boot. They behave like `/boot/loader.conf` entries and only take effect
at the next load, so a later bare `kldload contigmem` inherits the
request that just failed.

A `setup` that fails after unloading the previous pool does not put
that pool back. On Linux, `--count 0` releases only the pool for the
size it resolves, which is a pre-existing difference from the FreeBSD
side, where it unloads the module and releases everything.
