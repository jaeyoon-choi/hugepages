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


def test_verbose_survives_logging_before_main_configures_it(monkeypatch):
    # parse_args() warns when the sizes cannot be read. That first log call
    # implicitly configures the root logger, which used to leave
    # basicConfig() a no-op and --verbose without effect.
    def _unreadable():
        raise OSError("sysfs unreadable")

    monkeypatch.setattr(hugepages, "list_supported_sizes", _unreadable)
    monkeypatch.setattr(sys, "argv", ["hugepages", "--verbose"])
    with pytest.raises(SystemExit):
        hugepages.main()
    assert log.getLogger().level == log.DEBUG
