# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) Simon Andreas Frimann Lund <os@safl.dk>

import argparse
import errno
import logging as log
import shlex
import subprocess
import sys

import pytest

from hugepages import hugepages

_512M = 512 * 1024 * 1024


def test_help():
    result = subprocess.run(
        [sys.executable, "-m", "hugepages.hugepages", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "hugepages" in result.stdout.lower()


def test_import():
    from hugepages import main

    assert callable(main)


def test_get_backend_dispatch():
    assert isinstance(hugepages.get_backend("Linux"), hugepages.LinuxBackend)
    assert isinstance(hugepages.get_backend("FreeBSD"), hugepages.FreeBSDBackend)
    assert hugepages.get_backend("Darwin") is None


def test_verbose_survives_logging_before_main_configures_it(monkeypatch):
    # Anything logged before main() calls basicConfig() implicitly configures
    # the root logger. --verbose must still take effect afterward.
    class _Backend:
        default_size = None

        def supported_sizes(self):
            raise OSError("sysfs unreadable")

    monkeypatch.setattr(log.getLogger(), "handlers", [])
    monkeypatch.setattr(hugepages, "get_backend", lambda: _Backend())
    monkeypatch.setattr(sys, "argv", ["hugepages", "--verbose"])
    with pytest.raises(SystemExit):
        hugepages.main()
    assert log.getLogger().level == log.DEBUG


def _completed(cmd, returncode, stdout="", stderr=""):
    return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)


class _FakeFreeBSD:
    """Stand-in for hugepages.run() that simulates contigmem kernel state.

    Mirrors the real module contract: tunables set with kenv apply only at
    kldload time, the hw.contigmem sysctls exist only while loaded, and
    kldunload fails while mappings still reference the pool.
    """

    PAGE_SIZE = 4096

    def __init__(self, loaded=None, references=0, kldload_error=None, kenv_ignored=False):
        self.kenv = {}
        self.loaded = loaded  # (num_buffers, buffer_size) or None
        self.references = references
        self.kldload_error = kldload_error
        self.kenv_ignored = kenv_ignored
        self.commands = []

    def __call__(self, cmd):
        key = shlex.join(cmd)
        self.commands.append(key)
        log.info(f"cmd({key})")  # mirror the trace the real run() emits
        handlers = {
            "sysctl": self._sysctl,
            "kenv": self._kenv,
            "kldload": self._kldload,
            "kldunload": self._kldunload,
        }
        handler = handlers.get(cmd[0])
        if handler is None:
            return _completed(cmd, 1, stderr=f"{cmd[0]}: not found")
        return handler(cmd)

    def _sysctl(self, cmd):
        name = cmd[-1]
        values = {"hw.pagesize": str(self.PAGE_SIZE)}
        if self.loaded is not None:
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
        name, _, value = cmd[1].partition("=")
        self.kenv[name] = value
        return _completed(cmd, 0, stdout=value + "\n")

    def _kldload(self, cmd):
        if self.kldload_error is not None:
            return _completed(cmd, 1, stderr=self.kldload_error)
        if self.loaded is not None:
            return _completed(cmd, 1, stderr="kldload: module already loaded or in kernel")
        kenv = {} if self.kenv_ignored else self.kenv
        self.loaded = (
            int(kenv.get("hw.contigmem.num_buffers", 1)),
            int(kenv.get("hw.contigmem.buffer_size", _512M)),
        )
        return _completed(cmd, 0)

    def _kldunload(self, cmd):
        if self.loaded is None:
            return _completed(cmd, 1, stderr="kldunload: can't find file contigmem")
        if self.references:
            return _completed(cmd, 1, stderr="kldunload: can't unload file: Device busy")
        self.loaded = None
        return _completed(cmd, 0)


@pytest.fixture
def freebsd(monkeypatch):
    """A FreeBSDBackend wired to a fresh _FakeFreeBSD, running as fake root"""

    def _make(**kwargs):
        fake = _FakeFreeBSD(**kwargs)
        monkeypatch.setattr(hugepages, "run", fake)
        monkeypatch.setattr(hugepages.os, "geteuid", lambda: 0)
        return hugepages.FreeBSDBackend(), fake

    return _make


def _setup_args(count, size="524288"):
    return argparse.Namespace(size=size, count=count)


def test_freebsd_setup_loads_contigmem(freebsd, capsys):
    backend, fake = freebsd()
    backend.setup(_setup_args(count=2))
    assert fake.loaded == (2, _512M)
    out = capsys.readouterr().out
    assert "Reserved 2 x 524288 kB buffer(s)" in out
    # persistence hint
    assert "hw.contigmem.num_buffers=2" in out
    assert 'contigmem_load="YES"' in out


def test_freebsd_setup_requires_root(monkeypatch):
    monkeypatch.setattr(hugepages.os, "geteuid", lambda: 1000)
    with pytest.raises(SystemExit) as excinfo:
        hugepages.FreeBSDBackend().setup(_setup_args(count=1))
    assert excinfo.value.code == errno.EPERM


def test_freebsd_setup_same_config_is_noop(freebsd, capsys):
    backend, fake = freebsd(loaded=(2, _512M))
    backend.setup(_setup_args(count=2))
    assert "already provides" in capsys.readouterr().out
    assert not any(cmd.startswith(("kldunload", "kldload")) for cmd in fake.commands)


def test_freebsd_setup_reconfigures_by_reloading(freebsd):
    backend, fake = freebsd(loaded=(1, _512M))
    backend.setup(_setup_args(count=4, size="1048576"))
    assert fake.loaded == (4, 1024 * 1024 * 1024)
    assert fake.commands.index("kldunload contigmem") < fake.commands.index("kldload contigmem")


def test_freebsd_setup_reload_blocked_by_mappings(freebsd, caplog):
    backend, fake = freebsd(loaded=(1, _512M), references=3)
    with pytest.raises(SystemExit) as excinfo:
        backend.setup(_setup_args(count=2))
    assert excinfo.value.code != 0
    assert fake.loaded == (1, _512M)  # pool untouched
    assert "3 mapping(s)" in caplog.text


def test_freebsd_setup_count_zero_releases(freebsd, capsys):
    # count(0) is the documented way to release the pool.
    backend, fake = freebsd(loaded=(1, _512M))
    backend.setup(_setup_args(count=0))
    assert fake.loaded is None
    assert "released" in capsys.readouterr().out


def test_freebsd_setup_count_zero_without_pool_is_noop(freebsd, capsys):
    backend, fake = freebsd()
    backend.setup(_setup_args(count=0))
    assert "nothing to release" in capsys.readouterr().out
    assert "kldunload contigmem" not in fake.commands


def test_freebsd_setup_rejects_non_power_of_two(freebsd):
    backend, fake = freebsd()
    with pytest.raises(SystemExit) as excinfo:
        backend.setup(_setup_args(count=1, size="300000"))
    assert excinfo.value.code != 0
    assert "kldload contigmem" not in fake.commands


def test_freebsd_setup_points_at_dpdk_when_module_is_missing(freebsd, caplog):
    error = "kldload: can't load contigmem: No such file or directory"
    backend, _ = freebsd(kldload_error=error)
    with pytest.raises(SystemExit):
        backend.setup(_setup_args(count=1))
    assert "pkg install dpdk" in caplog.text


def test_freebsd_setup_suggests_loader_conf_when_load_fails(freebsd, caplog):
    backend, _ = freebsd(kldload_error="kldload: Cannot allocate memory")
    with pytest.raises(SystemExit):
        backend.setup(_setup_args(count=2))
    assert "/boot/loader.conf" in caplog.text
    assert "hw.contigmem.buffer_size=536870912" in caplog.text


def test_freebsd_setup_fails_when_tunables_do_not_stick(freebsd, caplog):
    # A loaded-but-mismatched pool must not pass as success.
    backend, _ = freebsd(kenv_ignored=True)
    with pytest.raises(SystemExit):
        backend.setup(_setup_args(count=2, size="1048576"))
    assert "not the requested" in caplog.text


def test_freebsd_setup_warns_past_the_module_buffer_cap(freebsd, caplog):
    backend, fake = freebsd()
    with caplog.at_level(log.WARNING):
        backend.setup(_setup_args(count=65))
    assert fake.loaded == (65, _512M)  # the module has the final say
    assert "cap of 64" in caplog.text


def test_freebsd_info_reports_pool(freebsd, capsys):
    backend, _ = freebsd(loaded=(2, _512M), references=1)
    backend.info(argparse.Namespace())
    out = capsys.readouterr().out
    assert "Buffers: 2" in out
    assert "Size: 524288 kB" in out
    assert "Total: 1048576 kB" in out
    assert "References: 1" in out
    assert "Buffer 0: physical address 0x180000000" in out
    assert "Buffer 1" in out


def test_freebsd_info_when_not_loaded(freebsd, capsys):
    backend, _ = freebsd()
    backend.info(argparse.Namespace())  # must not raise: "not loaded" is a state
    out = capsys.readouterr().out
    assert "not loaded" in out
    assert "hugepages setup" in out


def test_freebsd_mount_reports_the_device(tmp_path, capsys, monkeypatch):
    device = tmp_path / "contigmem"
    device.touch()
    monkeypatch.setattr(hugepages.FreeBSDBackend, "DEVICE", device)
    hugepages.FreeBSDBackend().mount(argparse.Namespace())
    out = capsys.readouterr().out
    assert "no hugetlbfs" in out.lower()
    assert str(device) in out
    assert "mmap" in out


def test_freebsd_mount_without_the_device(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(hugepages.FreeBSDBackend, "DEVICE", tmp_path / "contigmem")
    hugepages.FreeBSDBackend().mount(argparse.Namespace())
    assert "hugepages setup" in capsys.readouterr().out


def test_freebsd_size_is_free_form_with_contigmem_default(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["hugepages", "setup", "--count", "1"])
    args = hugepages.parse_args(hugepages.FreeBSDBackend())
    assert args.size == "524288"  # contigmem's own 512 MiB default

    monkeypatch.setattr(sys, "argv", ["hugepages", "setup", "--count", "1", "--size", "8192"])
    args = hugepages.parse_args(hugepages.FreeBSDBackend())
    assert args.size == "8192"  # no fixed choices; setup() validates


def test_freebsd_setup_requires_count(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["hugepages", "setup"])
    with pytest.raises(SystemExit):
        hugepages.parse_args(hugepages.FreeBSDBackend())


def test_verbose_surfaces_command_traces(freebsd, monkeypatch, capsys):
    # run() traces each command at INFO; --verbose must surface them.
    _, fake = freebsd(loaded=(1, _512M))
    monkeypatch.setattr(log.getLogger(), "handlers", [])
    monkeypatch.setattr(hugepages.platform, "system", lambda: "FreeBSD")
    monkeypatch.setattr(sys, "argv", ["hugepages", "--verbose", "info"])
    hugepages.main()
    assert "cmd(sysctl -n hw.contigmem.num_buffers)" in capsys.readouterr().err
