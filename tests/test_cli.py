# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) Simon Andreas Frimann Lund <os@safl.dk>

import argparse
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


class _FakeSysctl:
    """Stand-in for hugepages.run() backed by a {command: output} table."""

    def __init__(self, table):
        self.table = table

    def __call__(self, cmd):
        key = shlex.join(cmd)
        log.info(f"cmd({key})")  # mirror the trace the real run() emits
        if key in self.table:
            return subprocess.CompletedProcess(cmd, 0, stdout=self.table[key], stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not found")


def _patch_sysctl(monkeypatch, table):
    monkeypatch.setattr(hugepages, "run", _FakeSysctl(table))


# sysctl(8) renders hw.pagesizes through the S_pagesizes formatter on FreeBSD
# 13+ ("{ 4096, 2097152 }", zeroes omitted); older releases print a plain
# space-separated array padded with zeroes up to MAXPAGESIZES. -n only drops
# the name prefix, it does not change either rendering.
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


def test_freebsd_info(monkeypatch, capsys):
    _patch_sysctl(
        monkeypatch,
        {
            "sysctl -n hw.pagesizes": "{ 4096, 2097152 }",
            "sysctl -n vm.pmap.pg_ps_enabled": "1",
            "sysctl -n vm.pmap.pde.mappings": "128",
            "sysctl -n vm.pmap.pde.promotions": "4096",
        },
    )
    hugepages.FreeBSDBackend().info(argparse.Namespace())
    out = capsys.readouterr().out
    assert "Superpages: enabled" in out
    assert "4096 bytes (4 kB)" in out
    assert "2097152 bytes (2048 kB)" in out
    assert "mappings: 128" in out


def test_freebsd_info_arm64_oids(monkeypatch, capsys):
    # arm64 spells the knob superpages_enabled and files counters under l2.
    _patch_sysctl(
        monkeypatch,
        {
            "sysctl -n hw.pagesizes": "{ 4096, 2097152 }",
            "sysctl -n vm.pmap.superpages_enabled": "1",
            "sysctl -n vm.pmap.l2.mappings": "7",
        },
    )
    hugepages.FreeBSDBackend().info(argparse.Namespace())
    out = capsys.readouterr().out
    assert "Superpages: enabled (vm.pmap.superpages_enabled=1)" in out
    assert "mappings: 7" in out


def test_freebsd_info_fails_loudly_when_sysctl_is_unreadable(monkeypatch):
    # No readable OID at all must not look like a successful empty report.
    _patch_sysctl(monkeypatch, {})
    with pytest.raises(SystemExit) as excinfo:
        hugepages.FreeBSDBackend().info(argparse.Namespace())
    assert excinfo.value.code != 0


def test_freebsd_setup_is_informational(monkeypatch, capsys):
    _patch_sysctl(monkeypatch, {"sysctl -n vm.pmap.pg_ps_enabled": "0"})
    # setup must not raise/exit on FreeBSD; it only explains the model.
    hugepages.FreeBSDBackend().setup(argparse.Namespace())
    out = capsys.readouterr().out
    assert "superpages" in out.lower()
    assert "currently disabled" in out


def test_freebsd_mount_is_informational(capsys):
    hugepages.FreeBSDBackend().mount(argparse.Namespace())
    out = capsys.readouterr().out
    assert "MAP_ALIGNED_SUPER" in out


def test_freebsd_setup_needs_no_count(monkeypatch):
    # README documents FreeBSD setup as informational, so it must parse with
    # no arguments; --count is only mandatory where pages are really reserved.
    monkeypatch.setattr(sys, "argv", ["hugepages", "setup"])
    args = hugepages.parse_args(hugepages.FreeBSDBackend())
    assert args.command == "setup"
    assert args.count is None


def test_linux_setup_still_requires_count(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["hugepages", "setup"])
    with pytest.raises(SystemExit):
        hugepages.parse_args(hugepages.LinuxBackend())


def test_verbose_survives_logging_before_main_configures_it(monkeypatch, capsys):
    # Anything logging before main() calls basicConfig() implicitly configures
    # the root logger, which would leave basicConfig() a no-op and silently
    # disable --verbose. Drive main() end to end and require DEBUG to stick.
    monkeypatch.setattr(log.getLogger(), "handlers", [])
    log.info("a stray log line before main() configures logging")

    _patch_sysctl(monkeypatch, {"sysctl -n vm.pmap.pg_ps_enabled": "1"})
    monkeypatch.setattr(hugepages.platform, "system", lambda: "FreeBSD")
    monkeypatch.setattr(sys, "argv", ["hugepages", "--verbose", "setup"])
    hugepages.main()

    assert log.getLogger().level == log.DEBUG
    # run() traces each command at INFO, so --verbose must surface them.
    assert "cmd(sysctl -n vm.pmap.pg_ps_enabled)" in capsys.readouterr().err


def test_get_backend_dispatch():
    assert isinstance(hugepages.get_backend("Linux"), hugepages.LinuxBackend)
    assert isinstance(hugepages.get_backend("FreeBSD"), hugepages.FreeBSDBackend)
    assert hugepages.get_backend("Darwin") is None
