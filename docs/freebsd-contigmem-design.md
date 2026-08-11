# Design: a FreeBSD backend backed by contigmem

This branch replaces the earlier FreeBSD backend, which only reported
transparent superpage state and treated `setup`/`mount` as
informational. With contigmem, FreeBSD gains a real reservable pool, so
the backend becomes configurable like the Linux one.

## Background

The Linux backend manages a real kernel object: the hugepage pool under
`/sys/kernel/mm/hugepages/`. FreeBSD has no such pool and no hugetlbfs;
its superpages are transparent and cannot be reserved.

The DPDK `contigmem` kernel module fills that gap. At load time it
reserves physically contiguous buffers and exposes them through the
`/dev/contigmem` device. Its contract, taken from the driver source
(`kernel/freebsd/contigmem/contigmem.c` in DPDK):

- Tunables, read once at load time: `hw.contigmem.num_buffers`
  (default 1) and `hw.contigmem.buffer_size` (bytes, default 512 MiB).
  At most 64 buffers (`RTE_CONTIGMEM_MAX_NUM_BUFS`).
- Read-only sysctls while loaded: `num_buffers`, `buffer_size`,
  `num_references`, and `physaddr.<i>` per buffer.
- The buffer size must be a power of two larger than `PAGE_SIZE`.
  The module rejects anything else at load time.
- Applications map buffer `i` by calling `mmap` on `/dev/contigmem`
  with offset `i * PAGE_SIZE`.
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

1. Require root up front (`os.geteuid()`). A deterministic check beats
   parsing kenv/kldload stderr. Same message and `EPERM` exit as Linux.
2. `--count 0`: unload if loaded; success without work otherwise.
   `EBUSY` reports how many mappings still reference the pool.
3. Validate locally what the driver validates: the size must be a
   power of two larger than the page size. A count above 64 only warns,
   because the cap is a compile-time constant the module enforces.
4. Already loaded: same configuration is a no-op; a different one
   unloads first, since tunables only apply at load time.
5. Set both tunables with `kenv`, then `kldload contigmem`. On failure,
   show stderr plus a targeted hint: install DPDK when the module file
   is missing, or the `/boot/loader.conf` lines when memory is likely
   too fragmented to allocate at runtime.
6. Read the pool back from sysctl and compare with the request. The
   load is all-or-nothing, so a mismatch is an error, not a partial
   reservation warning like on Linux.
7. Print a summary and the `/boot/loader.conf` lines for persistence.

## Interface changes

- `supported_sizes()` may now return an empty list, meaning --size is
  free-form and `setup` validates it. Fixed choices would be artificial
  for contigmem, which takes any power of two.
- Backends gain `default_size`, the --size default when there are no
  choices. FreeBSD uses 524288 kB, contigmem's own 512 MiB default.
- No `configurable` flag: both backends reserve a real pool, so
  `--count` stays required everywhere.

## Decisions

- Superpage reporting is gone. The backend covers contigmem only.
- The tool never writes `/boot/loader.conf`; it prints the lines. A
  `--persist` flag can be added later if wanted.
- `mount` stays informational and state-aware: there is nothing to
  mount, so it points at `/dev/contigmem` and how to map it.
- Root is checked before doing work, not inferred from tool stderr.

## Testing

Tests replace the command table used earlier with a stateful fake
(`_FakeFreeBSD`) that simulates the driver contract: kenv values apply
only at kldload time, the `hw.contigmem` sysctls exist only while
loaded, and kldunload fails while references remain. This lets the
order-sensitive flows -- reconfigure-by-reload, readback verification,
busy unload -- run against the same rules the kernel enforces.
