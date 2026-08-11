# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) Simon Andreas Frimann Lund <os@safl.dk>

import logging as log
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


def test_get_backend_dispatch():
    assert isinstance(hugepages.get_backend("Linux"), hugepages.LinuxBackend)
    assert hugepages.get_backend("Darwin") is None


def test_verbose_survives_logging_before_main_configures_it(monkeypatch):
    # Anything logged before main() calls basicConfig() implicitly configures
    # the root logger. --verbose must still take effect afterward.
    class _Backend:
        def supported_sizes(self):
            raise OSError("sysfs unreadable")

    monkeypatch.setattr(log.getLogger(), "handlers", [])
    monkeypatch.setattr(hugepages, "get_backend", lambda: _Backend())
    monkeypatch.setattr(sys, "argv", ["hugepages", "--verbose"])
    with pytest.raises(SystemExit):
        hugepages.main()
    assert log.getLogger().level == log.DEBUG
