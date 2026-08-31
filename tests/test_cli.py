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


KLDSTAT = "kldstat -q -m contigmem"


class _FakeRun:
    """Stand-in for hugepages.run() backed by a {command: result} table.

    A value is either a plain string (stdout of a successful command) or a
    (returncode, stdout, stderr) tuple for failure paths. Every call is
    recorded in .calls so tests can assert command order.
    """

    def __init__(self, table):
        self.table = table
        self.calls = []

    def __call__(self, cmd):
        key = shlex.join(cmd)
        self.calls.append(key)
        log.info(f"cmd({key})")  # mirror the trace the real run() emits
        if key not in self.table:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not found")
        value = self.table[key]
        if isinstance(value, str):
            return subprocess.CompletedProcess(cmd, 0, stdout=value, stderr="")
        returncode, stdout, stderr = value
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)


def _patch_run(monkeypatch, table):
    fake = _FakeRun(table)
    monkeypatch.setattr(hugepages, "run", fake)
    # Faked commands imply a faked root; the requires-root tests override this.
    monkeypatch.setattr(hugepages.os, "geteuid", lambda: 0)
    return fake


# sysctl -n renders hw.pagesizes in two formats. See parse_pagesizes().
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
    assert hugepages.FreeBSDBackend.parse_pagesizes(raw) == expected


def test_freebsd_info_not_loaded(monkeypatch, capsys):
    _patch_run(monkeypatch, {"sysctl -n hw.pagesizes": "{ 4096, 2097152 }"})
    hugepages.FreeBSDBackend().info(argparse.Namespace())
    out = capsys.readouterr().out
    assert "contigmem: not loaded" in out
    assert "pkg search dpdk" in out  # a fresh system learns the install path here
    assert "4096 bytes (4 kB)" in out


def test_freebsd_info_loaded(monkeypatch, capsys):
    _patch_run(
        monkeypatch,
        {
            "sysctl -n hw.pagesizes": "{ 4096, 2097152 }",
            KLDSTAT: "",
            "sysctl -n hw.contigmem.num_buffers": "2",
            "sysctl -n hw.contigmem.buffer_size": "1073741824",
            "sysctl -n hw.contigmem.num_references": "1",
            "sysctl -n hw.contigmem.physaddr.0": "6442450944",
            "sysctl -n hw.contigmem.physaddr.1": "10737418240",
        },
    )
    hugepages.FreeBSDBackend().info(argparse.Namespace())
    out = capsys.readouterr().out
    assert "contigmem: loaded" in out
    assert "Buffers: 2 x 1073741824 bytes (1048576 kB)" in out
    assert "Mapped references: 1" in out
    assert "Buffer 0: physaddr 0x180000000" in out
    assert "Buffer 1: physaddr 0x280000000" in out


def test_freebsd_info_reports_superpage_alignment(monkeypatch, capsys):
    # 0x180000000 divides by 1 GiB. 0x238000000 divides by 2 MiB only.
    _patch_run(
        monkeypatch,
        {
            "sysctl -n hw.pagesizes": "{ 4096, 2097152, 1073741824 }",
            KLDSTAT: "",
            "sysctl -n hw.contigmem.num_buffers": "2",
            "sysctl -n hw.contigmem.buffer_size": "134217728",
            "sysctl -n hw.contigmem.physaddr.0": "6442450944",
            "sysctl -n hw.contigmem.physaddr.1": "9529458688",
        },
    )
    hugepages.FreeBSDBackend().info(argparse.Namespace())
    out = capsys.readouterr().out
    assert "Buffer 0: physaddr 0x180000000 (1 GiB aligned)" in out
    assert "Buffer 1: physaddr 0x238000000 (2 MiB aligned)" in out


def test_freebsd_setup_warns_below_the_smallest_large_page(monkeypatch, caplog):
    _patch_run(
        monkeypatch,
        {
            "sysctl -n hw.pagesizes": "{ 4096, 2097152 }",
            "kenv hw.contigmem.num_buffers=1": "1",
            "kenv hw.contigmem.buffer_size=1048576": "1048576",
            "kldload contigmem": "",
            "sysctl -n hw.contigmem.num_buffers": "1",
        },
    )
    with caplog.at_level(log.WARNING):
        hugepages.FreeBSDBackend().setup(argparse.Namespace(size="1024", count=1))
    assert "smaller than the smallest large page" in caplog.text
    assert "2048 kB" in caplog.text


def test_freebsd_info_fails_cleanly_on_garbage_sysctl(monkeypatch):
    # sysctl can exit 0 with unparsable stdout. That must not raise ValueError.
    _patch_run(
        monkeypatch,
        {
            KLDSTAT: "",
            "sysctl -n hw.contigmem.num_buffers": "2",
            "sysctl -n hw.contigmem.buffer_size": "",
        },
    )
    with pytest.raises(SystemExit) as excinfo:
        hugepages.FreeBSDBackend().info(argparse.Namespace())
    assert excinfo.value.code != 0


def test_freebsd_info_fails_loudly_when_contigmem_is_unreadable(monkeypatch):
    # A loaded module with no readable sysctl must not look like a success.
    _patch_run(monkeypatch, {KLDSTAT: ""})
    with pytest.raises(SystemExit) as excinfo:
        hugepages.FreeBSDBackend().info(argparse.Namespace())
    assert excinfo.value.code != 0


def test_freebsd_mount_is_informational(capsys):
    hugepages.FreeBSDBackend().mount(argparse.Namespace())
    out = capsys.readouterr().out
    assert "/dev/contigmem" in out


def test_freebsd_setup_loads_contigmem(monkeypatch, capsys):
    fake = _patch_run(
        monkeypatch,
        {
            "kenv hw.contigmem.num_buffers=2": "2",
            "kenv hw.contigmem.buffer_size=536870912": "536870912",
            "kldload contigmem": "",
            "sysctl -n hw.contigmem.num_buffers": "2",
        },
    )
    hugepages.FreeBSDBackend().setup(argparse.Namespace(size="524288", count=2))
    out = capsys.readouterr().out
    assert "Reserved 2 x 524288 kB" in out
    assert "hw.contigmem.num_buffers=2" in out
    assert "hw.contigmem.buffer_size=536870912" in out
    assert 'contigmem_load="YES"' in out
    assert "kldunload contigmem" not in fake.calls
    # Tunables must be in place before the module loads.
    kenv_at = fake.calls.index("kenv hw.contigmem.num_buffers=2")
    assert kenv_at < fake.calls.index("kldload contigmem")


def test_freebsd_setup_reloads_when_already_loaded(monkeypatch, capsys):
    # Tunables are read only at load time, so resizing must reload.
    fake = _patch_run(
        monkeypatch,
        {
            KLDSTAT: "",
            "kldunload contigmem": "",
            "kenv hw.contigmem.num_buffers=1": "1",
            "kenv hw.contigmem.buffer_size=1073741824": "1073741824",
            "kldload contigmem": "",
            "sysctl -n hw.contigmem.num_buffers": "1",
        },
    )
    hugepages.FreeBSDBackend().setup(argparse.Namespace(size="1048576", count=1))
    assert "Reserved 1 x 1048576 kB" in capsys.readouterr().out
    unload_at = fake.calls.index("kldunload contigmem")
    assert unload_at < fake.calls.index("kenv hw.contigmem.num_buffers=1")


def test_freebsd_setup_count_zero_releases(monkeypatch, capsys):
    fake = _patch_run(monkeypatch, {KLDSTAT: "", "kldunload contigmem": ""})
    hugepages.FreeBSDBackend().setup(argparse.Namespace(size=None, count=0))
    assert "Released" in capsys.readouterr().out
    assert "kldunload contigmem" in fake.calls


def test_freebsd_setup_count_zero_when_not_loaded(monkeypatch, capsys):
    fake = _patch_run(monkeypatch, {})
    hugepages.FreeBSDBackend().setup(argparse.Namespace(size=None, count=0))
    assert "nothing to release" in capsys.readouterr().out
    assert "kldunload contigmem" not in fake.calls


def test_freebsd_setup_rejects_non_power_of_two(monkeypatch):
    fake = _patch_run(monkeypatch, {})
    with pytest.raises(SystemExit) as excinfo:
        hugepages.FreeBSDBackend().setup(argparse.Namespace(size="1000", count=1))
    assert excinfo.value.code != 0
    assert fake.calls == []  # rejected before any command runs


def test_freebsd_setup_requires_root(monkeypatch, caplog):
    # FreeBSD's kenv prints no errno text on failure, so setup must not rely
    # on stderr matching: it checks the euid before running any command.
    fake = _patch_run(monkeypatch, {})
    monkeypatch.setattr(hugepages.os, "geteuid", lambda: 1000)
    with pytest.raises(SystemExit) as excinfo, caplog.at_level(log.ERROR):
        hugepages.FreeBSDBackend().setup(argparse.Namespace(size="524288", count=1))
    # EPERM == 1 == the generic exit code, so pin the sudo hint too.
    assert excinfo.value.code == errno.EPERM
    assert "Re-run with sudo" in caplog.text
    assert fake.calls == []  # denied before any command runs


def test_freebsd_setup_release_requires_root(monkeypatch, caplog):
    fake = _patch_run(monkeypatch, {KLDSTAT: ""})
    monkeypatch.setattr(hugepages.os, "geteuid", lambda: 1000)
    with pytest.raises(SystemExit) as excinfo, caplog.at_level(log.ERROR):
        hugepages.FreeBSDBackend().setup(argparse.Namespace(size=None, count=0))
    assert excinfo.value.code == errno.EPERM
    assert "kldunload contigmem" not in fake.calls


def test_freebsd_setup_reports_missing_module(monkeypatch, caplog):
    _patch_run(
        monkeypatch,
        {
            "kenv hw.contigmem.num_buffers=1": "1",
            "kenv hw.contigmem.buffer_size=536870912": "536870912",
            "kldload contigmem": (1, "", "kldload: can't load contigmem: No such file"),
        },
    )
    with pytest.raises(SystemExit) as excinfo, caplog.at_level(log.ERROR):
        hugepages.FreeBSDBackend().setup(argparse.Namespace(size="524288", count=1))
    assert excinfo.value.code != 0
    assert "pkg install dpdk" in caplog.text


def test_freebsd_setup_release_fails_while_mapped(monkeypatch, caplog):
    _patch_run(
        monkeypatch,
        {
            KLDSTAT: "",
            "kldunload contigmem": (1, "", "kldunload: can't unload file: Device busy"),
        },
    )
    with pytest.raises(SystemExit) as excinfo, caplog.at_level(log.ERROR):
        hugepages.FreeBSDBackend().setup(argparse.Namespace(size=None, count=0))
    assert excinfo.value.code != 0
    assert "mapped" in caplog.text
    assert "fstat /dev/contigmem" in caplog.text  # name the tool that finds it


def test_freebsd_setup_reload_failure_names_lost_reservation(monkeypatch, caplog):
    _patch_run(
        monkeypatch,
        {
            KLDSTAT: "",
            "kldunload contigmem": "",
            "kenv hw.contigmem.num_buffers=4": "4",
            "kenv hw.contigmem.buffer_size=1073741824": "1073741824",
            "kldload contigmem": (1, "", "kldload: an error occurred while loading module"),
        },
    )
    with pytest.raises(SystemExit) as excinfo, caplog.at_level(log.ERROR):
        hugepages.FreeBSDBackend().setup(argparse.Namespace(size="1048576", count=4))
    assert excinfo.value.code != 0
    assert "previous reservation" in caplog.text


def test_linux_setup_without_any_size_fails_cleanly(caplog):
    # A kernel without hugetlb support yields no sysfs sizes and size=None.
    with pytest.raises(SystemExit) as excinfo, caplog.at_level(log.ERROR):
        hugepages.LinuxBackend().setup(argparse.Namespace(size=None, count=4))
    assert excinfo.value.code != 0
    assert "no hugepage support" in caplog.text


def test_freebsd_setup_now_requires_count(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["hugepages", "setup"])
    with pytest.raises(SystemExit):
        hugepages.parse_args(hugepages.FreeBSDBackend())


def test_freebsd_size_defaults_to_contigmem_default(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["hugepages", "setup", "--count", "1"])
    args = hugepages.parse_args(hugepages.FreeBSDBackend())
    assert args.size == "524288"
    assert args.count == 1


def test_linux_setup_still_requires_count(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["hugepages", "setup"])
    with pytest.raises(SystemExit):
        hugepages.parse_args(hugepages.LinuxBackend())


def test_verbose_survives_logging_before_main_configures_it(monkeypatch, capsys):
    # Anything logging before main() calls basicConfig() implicitly configures
    # the root logger, which would leave basicConfig() a no-op and silently
    # disable --verbose. Drive main() end to end and require DEBUG to stick.
    root = log.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(root, "level", root.level)  # undo basicConfig's DEBUG afterward
    log.info("a stray log line before main() configures logging")

    _patch_run(monkeypatch, {})
    monkeypatch.setattr(hugepages.platform, "system", lambda: "FreeBSD")
    monkeypatch.setattr(sys, "argv", ["hugepages", "--verbose", "setup", "--count", "0"])
    hugepages.main()

    assert log.getLogger().level == log.DEBUG
    # run() traces each command at INFO, so --verbose must surface them.
    assert "cmd(kldstat -q -m contigmem)" in capsys.readouterr().err


def test_get_backend_dispatch():
    assert isinstance(hugepages.get_backend("Linux"), hugepages.LinuxBackend)
    assert isinstance(hugepages.get_backend("FreeBSD"), hugepages.FreeBSDBackend)
    assert hugepages.get_backend("Darwin") is None
