# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) Simon Andreas Frimann Lund <os@safl.dk>
# Copyright (c) Jaeyoon Choi <j_yoon.choi@samsung.com>
"""Unit tests for the FreeBSD platform (runnable on any platform)."""

import errno
import logging as log
import shlex
import subprocess
import sys

import pytest

from hugepages import hugepages


_512M = 512 * 1024 * 1024


def _completed(cmd, returncode, stdout="", stderr=""):
    return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)


class _FakeFreeBSD:
    """Stand-in for hugepages.run() that simulates contigmem kernel state.

    Mirrors the real module contract: tunables set with kenv apply only at
    kldload time, the hw.contigmem sysctls exist only while loaded, and
    kldunload fails while mappings still reference the pool.
    """

    PAGE_SIZE = 4096

    def __init__(
        self,
        loaded=None,
        references=0,
        kldload_error=None,
        kenv_ignored=False,
        sysctl_broken=False,
        permitted=True,
        kld_permitted=True,
        missing=(),
    ):
        self.kenv = {}
        self.loaded = loaded  # (num_buffers, buffer_size) or None
        self.references = references
        self.kldload_error = kldload_error
        self.kenv_ignored = kenv_ignored
        self.sysctl_broken = sysctl_broken
        self.permitted = permitted  # False mimics running without root
        self.kld_permitted = kld_permitted  # False mimics securelevel: only kld* denied
        self.missing = set(missing)  # binaries absent from PATH
        self.commands = []

    def __call__(self, cmd):
        key = shlex.join(cmd)
        self.commands.append(key)
        log.debug(f"cmd({key})")  # mirror the trace the real run() emits
        handlers = {
            "sysctl": self._sysctl,
            "kenv": self._kenv,
            "kldstat": self._kldstat,
            "kldload": self._kldload,
            "kldunload": self._kldunload,
        }
        handler = handlers.get(cmd[0])
        if handler is None or cmd[0] in self.missing:
            # Mirror run(): a missing binary is exit 127 with the errno text.
            return _completed(cmd, 127, stderr=f"[Errno 2] No such file or directory: '{cmd[0]}'")
        return handler(cmd)

    def _kldstat(self, cmd):
        # kldstat -q -m contigmem: exits 0 iff the module is loaded.
        return _completed(cmd, 0 if self.loaded is not None else 1)

    def _sysctl(self, cmd):
        name = cmd[-1]
        values = {
            "hw.pagesize": str(self.PAGE_SIZE),
            "hw.pagesizes": "{ 4096, 2097152 }",
        }
        if self.loaded is not None and not self.sysctl_broken:
            num, size = self.loaded
            values["hw.contigmem.num_buffers"] = str(num)
            values["hw.contigmem.buffer_size"] = str(size)
            values["hw.contigmem.num_references"] = str(self.references)
            for index in range(num):
                values[f"hw.contigmem.physaddr.{index}"] = str(0x180000000 + index * size)
        if name not in values:
            return _completed(cmd, 1, stderr=f"sysctl: unknown oid '{name}'")
        return _completed(cmd, 0, stdout=values[name] + "\n")

    def _kenv(self, cmd):
        if not self.permitted:
            return _completed(cmd, 1, stderr="kenv: Operation not permitted")
        name, _, value = cmd[1].partition("=")
        self.kenv[name] = value
        return _completed(cmd, 0, stdout=value + "\n")

    def _kldload(self, cmd):
        if not (self.permitted and self.kld_permitted):
            return _completed(cmd, 1, stderr="kldload: Operation not permitted")
        if self.kldload_error is not None:
            return _completed(cmd, 1, stderr=self.kldload_error)
        if self.loaded is not None:
            return _completed(cmd, 1, stderr="kldload: module already loaded or in kernel")
        kenv = {} if self.kenv_ignored else self.kenv
        num = int(kenv.get("hw.contigmem.num_buffers", 1))
        size = int(kenv.get("hw.contigmem.buffer_size", _512M))
        if num > 64:
            # The module rejects loads past RTE_CONTIGMEM_MAX_NUM_BUFS with EINVAL.
            return _completed(cmd, 1, stderr="kldload: can't load contigmem: Invalid argument")
        self.loaded = (num, size)
        return _completed(cmd, 0)

    def _kldunload(self, cmd):
        if not (self.permitted and self.kld_permitted):
            return _completed(cmd, 1, stderr="kldunload: Operation not permitted")
        if self.loaded is None:
            return _completed(cmd, 1, stderr="kldunload: can't find file contigmem")
        if self.references:
            return _completed(cmd, 1, stderr="kldunload: can't unload file: Device busy")
        self.loaded = None
        return _completed(cmd, 0)


@pytest.fixture
def freebsd(monkeypatch):
    """A FreeBSD platform wired to a fresh _FakeFreeBSD"""

    def _make(**kwargs):
        fake = _FakeFreeBSD(**kwargs)
        monkeypatch.setattr(hugepages, "run", fake)
        return hugepages.FreeBSD(), fake

    return _make


# sysctl(8) renders hw.pagesizes through the S_pagesizes formatter on FreeBSD
# 13+ ("{ 4096, 2097152 }", zeroes omitted); older releases print a plain
# space-separated array padded with zeroes up to MAXPAGESIZES.
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("{ 4096, 2097152, 1073741824 }", [4096, 2097152, 1073741824]),
        ("{ 4096, 2097152 }", [4096, 2097152]),
        ("{ 4096 }", [4096]),
        ("4096 2097152 0 0", [4096, 2097152]),
        ("4096 0", [4096]),
        ("", []),
        (None, []),
    ],
)
def test_parse_pagesizes_handles_both_sysctl_formats(raw, expected):
    assert hugepages.FreeBSD._parse_pagesizes(raw) == expected


def test_setup_loads_contigmem(freebsd, capsys):
    plat, fake = freebsd()
    plat.setup(None, 2)
    assert fake.loaded == (2, _512M)
    out = capsys.readouterr().out
    assert "Reserved 2 x 524288 kB buffer(s)" in out
    # persistence hint
    assert "hw.contigmem.num_buffers=2" in out
    assert 'contigmem_load="YES"' in out


def test_setup_requires_root(freebsd, monkeypatch):
    # Root is inferred from the failing command, not checked up front.
    monkeypatch.setattr(hugepages.os, "geteuid", lambda: 1000)
    plat, _ = freebsd(permitted=False)
    with pytest.raises(PermissionError) as excinfo:
        plat.setup(None, 1)
    assert excinfo.value.errno == errno.EPERM
    assert "requires root" in excinfo.value.strerror
    assert "not permitted" in excinfo.value.strerror  # the raw stderr is preserved


def test_release_requires_root(freebsd, monkeypatch):
    monkeypatch.setattr(hugepages.os, "geteuid", lambda: 1000)
    plat, fake = freebsd(loaded=(1, _512M), permitted=False)
    with pytest.raises(PermissionError) as excinfo:
        plat.setup(None, 0)
    assert excinfo.value.errno == errno.EPERM
    assert "requires root" in excinfo.value.strerror
    assert fake.loaded == (1, _512M)


def test_setup_explains_kernel_refusal_to_root(freebsd, monkeypatch):
    # EPERM hits root too (securelevel >= 1, jails); sudo advice would
    # send an already-root admin down a dead end.
    monkeypatch.setattr(hugepages.os, "geteuid", lambda: 0)
    plat, _ = freebsd(kld_permitted=False)
    with pytest.raises(PermissionError) as excinfo:
        plat.setup(None, 1)
    assert excinfo.value.errno == errno.EPERM
    assert "securelevel" in excinfo.value.strerror
    assert "sudo" not in excinfo.value.strerror


def test_release_without_pool_needs_no_root(freebsd, capsys):
    # Nothing to unload, so no privileged command runs.
    plat, fake = freebsd(permitted=False)
    plat.setup(None, 0)
    assert "nothing to release" in capsys.readouterr().out
    assert not any(cmd.startswith(("kldunload", "kenv")) for cmd in fake.commands)


def test_setup_same_config_is_noop(freebsd, capsys):
    plat, fake = freebsd(loaded=(2, _512M))
    plat.setup(None, 2)
    assert "already provides" in capsys.readouterr().out
    assert not any(cmd.startswith(("kldunload", "kldload")) for cmd in fake.commands)


def test_setup_reconfigures_by_reloading(freebsd):
    plat, fake = freebsd(loaded=(1, _512M))
    plat.setup(1048576, 4)
    assert fake.loaded == (4, 1024 * 1024 * 1024)
    assert fake.commands.index("kldunload contigmem") < fake.commands.index("kldload contigmem")


def test_setup_reload_blocked_by_mappings(freebsd):
    plat, fake = freebsd(loaded=(1, _512M), references=3)
    with pytest.raises(OSError) as excinfo:
        plat.setup(None, 2)
    assert excinfo.value.errno == errno.EBUSY
    assert fake.loaded == (1, _512M)  # pool untouched
    assert "3 mapping(s)" in excinfo.value.strerror


def test_setup_count_zero_releases(freebsd, capsys):
    # count(0) is the documented way to release the pool.
    plat, fake = freebsd(loaded=(1, _512M))
    plat.setup(None, 0)
    assert fake.loaded is None
    assert "released" in capsys.readouterr().out


def test_setup_count_zero_without_pool_is_noop(freebsd, capsys):
    plat, fake = freebsd()
    plat.setup(None, 0)
    assert "nothing to release" in capsys.readouterr().out
    assert "kldunload contigmem" not in fake.commands


def test_setup_accepts_page_sized_buffer(freebsd):
    # The module accepts a size equal to the page size; its check is >=.
    plat, fake = freebsd()
    plat.setup(4, 1)
    assert fake.loaded == (1, 4096)


def test_setup_rejects_non_power_of_two(freebsd):
    plat, fake = freebsd()
    with pytest.raises(OSError) as excinfo:
        plat.setup(300000, 1)
    assert excinfo.value.errno == errno.EINVAL
    assert "kldload contigmem" not in fake.commands


def test_setup_names_the_missing_binary_not_the_module(freebsd):
    # run() reports a missing kldload as 127 with "No such file or
    # directory", which must not be read as a missing contigmem.ko.
    plat, fake = freebsd()
    fake.missing = {"kldload"}
    with pytest.raises(OSError) as excinfo:
        plat.setup(None, 1)
    assert "kldload(8) could not be run" in excinfo.value.strerror
    assert "pkg install dpdk" not in excinfo.value.strerror


def test_setup_points_at_dpdk_when_module_is_missing(freebsd):
    error = "kldload: can't load contigmem: No such file or directory"
    plat, _ = freebsd(kldload_error=error)
    with pytest.raises(OSError) as excinfo:
        plat.setup(None, 1)
    assert "pkg install dpdk" in excinfo.value.strerror


def test_setup_suggests_loader_conf_when_load_fails(freebsd):
    plat, _ = freebsd(kldload_error="kldload: Cannot allocate memory")
    with pytest.raises(OSError) as excinfo:
        plat.setup(None, 2)
    assert "/boot/loader.conf" in excinfo.value.strerror
    assert "hw.contigmem.buffer_size=536870912" in excinfo.value.strerror


def test_setup_skips_the_loader_conf_hint_on_module_rejection(freebsd):
    # Persisting a configuration the module just rejected would fail at
    # boot the same way.
    plat, _ = freebsd(kldload_error="kldload: can't load contigmem: Invalid argument")
    with pytest.raises(OSError) as excinfo:
        plat.setup(None, 1)
    assert "rejected this configuration" in excinfo.value.strerror
    assert "loader.conf" not in excinfo.value.strerror


def test_setup_says_the_previous_pool_is_gone(freebsd):
    plat, fake = freebsd(loaded=(2, _512M))
    fake.kldload_error = "kldload: Cannot allocate memory"
    with pytest.raises(OSError) as excinfo:
        plat.setup(1048576, 4)
    assert fake.loaded is None
    assert "previous pool of 2 x 524288 kB buffer(s) was released" in excinfo.value.strerror


def test_setup_fails_when_tunables_do_not_stick(freebsd):
    # A loaded-but-mismatched pool must not pass as success.
    plat, _ = freebsd(kenv_ignored=True)
    with pytest.raises(OSError) as excinfo:
        plat.setup(1048576, 2)
    assert "not the requested" in excinfo.value.strerror


def test_setup_rejects_count_past_the_module_cap(freebsd):
    # The module rejects an over-cap load with EINVAL, so attempting it
    # would first unload a working pool for nothing. Refuse up front.
    plat, fake = freebsd(loaded=(2, _512M))
    with pytest.raises(OSError) as excinfo:
        plat.setup(None, 65)
    assert excinfo.value.errno == errno.EINVAL
    assert fake.loaded == (2, _512M)  # existing pool untouched
    assert "kldunload contigmem" not in fake.commands
    assert "cap of 64" in excinfo.value.strerror


def test_setup_rejects_negative_count(freebsd):
    plat, fake = freebsd()
    with pytest.raises(OSError) as excinfo:
        plat.setup(None, -1)
    assert "Invalid count: -1" in excinfo.value.strerror
    assert not fake.commands  # rejected before any command runs


def test_setup_does_not_touch_an_unreadable_pool(freebsd):
    # A pool kldstat sees but whose sysctls do not read back cannot be
    # compared against the request; unloading it would be destructive.
    plat, fake = freebsd(loaded=(1, _512M), sysctl_broken=True)
    with pytest.raises(OSError) as excinfo:
        plat.setup(None, 1)
    assert excinfo.value.errno == errno.EIO
    assert fake.loaded == (1, _512M)
    assert "kldunload contigmem" not in fake.commands


def test_info_reports_pool(freebsd, capsys):
    plat, _ = freebsd(loaded=(2, _512M), references=1)
    plat.info()
    out = capsys.readouterr().out
    assert "Page sizes: 4 kB, 2048 kB" in out
    assert "Buffers: 2" in out
    assert "Size: 524288 kB" in out
    assert "Total: 1048576 kB" in out
    assert "References: 1" in out
    assert "Buffer 0: physical address 0x180000000" in out
    assert "Buffer 1" in out


def test_info_fails_loudly_when_contigmem_is_unreadable(freebsd):
    # A loaded module with unreadable sysctls must not read as "not loaded".
    plat, _ = freebsd(loaded=(1, _512M), sysctl_broken=True)
    with pytest.raises(OSError) as excinfo:
        plat.info()
    assert excinfo.value.errno == errno.EIO


def test_info_when_not_loaded(freebsd, capsys):
    plat, _ = freebsd()
    plat.info()  # must not raise: "not loaded" is a state
    out = capsys.readouterr().out
    assert "Page sizes: 4 kB, 2048 kB" in out
    assert "not loaded" in out
    assert "hugepages setup" in out


def test_mount_reports_the_device(tmp_path, capsys, monkeypatch):
    device = tmp_path / "contigmem"
    device.touch()
    monkeypatch.setattr(hugepages.FreeBSD, "DEVICE", device)
    hugepages.FreeBSD().mount()
    out = capsys.readouterr().out
    assert "no hugetlbfs" in out.lower()
    assert str(device) in out
    assert "mmap" in out


def test_mount_fails_without_the_device(tmp_path, monkeypatch):
    # Exiting 0 here would let "hugepages mount && start_app" proceed with
    # no pool at all.
    monkeypatch.setattr(hugepages.FreeBSD, "DEVICE", tmp_path / "contigmem")
    with pytest.raises(OSError) as excinfo:
        hugepages.FreeBSD().mount()
    assert excinfo.value.errno == errno.ENOENT
    assert "hugepages setup" in excinfo.value.strerror


def test_mount_warns_about_the_linux_only_flags(tmp_path, monkeypatch, caplog):
    device = tmp_path / "contigmem"
    device.touch()
    monkeypatch.setattr(hugepages.FreeBSD, "DEVICE", device)
    with caplog.at_level(log.WARNING):
        hugepages.FreeBSD().mount("/mnt/huge", "2048")
    assert "ignored" in caplog.text


def test_setup_defaults_to_the_contigmem_buffer_size(freebsd, capsys):
    # No --size given: the platform picks contigmem's own 512 MiB default.
    plat, fake = freebsd()
    plat.setup(None, 1)
    assert fake.loaded == (1, _512M)
    assert "524288 kB" in capsys.readouterr().out


def test_parser_leaves_the_size_to_the_platform(monkeypatch):
    # No fixed choices and no default: the platform validates and resolves.
    monkeypatch.setattr(sys, "argv", ["hugepages", "setup", "--count", "1"])
    assert hugepages.parse_args().size is None

    monkeypatch.setattr(sys, "argv", ["hugepages", "setup", "--count", "1", "--size", "8192"])
    assert hugepages.parse_args().size == 8192


def test_setup_requires_count(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["hugepages", "setup"])
    with pytest.raises(SystemExit):
        hugepages.parse_args()


def test_command_traces_need_verbose(freebsd, monkeypatch, caplog, fake_system):
    # run() traces each command at DEBUG, so only --verbose shows them.
    trace = "cmd(sysctl -n hw.contigmem.num_buffers)"
    freebsd(loaded=(1, _512M))
    fake_system("FreeBSD")
    caplog.set_level(log.DEBUG)  # let every record through to the fixture

    monkeypatch.setattr(sys, "argv", ["hugepages", "info"])
    hugepages.main()
    assert trace not in caplog.text  # main() raised the level back to INFO

    caplog.clear()
    monkeypatch.setattr(sys, "argv", ["hugepages", "--verbose", "info"])
    hugepages.main()
    assert trace in caplog.text


def test_setup_says_the_previous_pool_is_gone_on_every_failure(freebsd, monkeypatch):
    # The permission path used to drop the warning about the lost pool.
    monkeypatch.setattr(hugepages.os, "geteuid", lambda: 1000)
    # Only the load is denied, so the unload really does destroy the pool.
    plat, fake = freebsd(loaded=(2, _512M), kldload_error="kldload: Operation not permitted")
    with pytest.raises(OSError) as excinfo:
        plat.setup(1048576, 4)
    assert fake.loaded is None  # the old pool really is gone
    assert "was released and is gone" in excinfo.value.strerror


def test_release_fails_loudly_when_kldstat_cannot_run(freebsd):
    # Reading that as "nothing to release" would report success over a pool
    # that is still reserved.
    plat, fake = freebsd(loaded=(1, _512M))
    fake.missing = {"kldstat"}
    with pytest.raises(OSError) as excinfo:
        plat.setup(None, 0)
    assert excinfo.value.errno == errno.ENOENT
    assert fake.loaded == (1, _512M)
