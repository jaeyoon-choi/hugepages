# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) Simon Andreas Frimann Lund <os@safl.dk>

import errno
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


def test_run_reports_a_missing_binary_as_a_failed_command():
    # Callers branch on a returncode, never on an exception.
    result = hugepages.run(["hugepages-no-such-binary"])
    assert result.returncode == 127
    assert result.stderr


def test_run_reports_a_non_executable_file_as_a_failed_command(tmp_path):
    tool = tmp_path / "tool"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o644)
    result = hugepages.run([str(tool)])
    assert result.returncode == 126
    assert result.stderr


def test_run_traces_commands_at_debug(caplog):
    # INFO is the default level, so traces must stay hidden without --verbose.
    caplog.set_level(log.INFO)
    hugepages.run(["hugepages-no-such-binary"])
    assert not caplog.records

    caplog.set_level(log.DEBUG)
    hugepages.run(["hugepages-no-such-binary"])
    assert any("cmd(" in record.message for record in caplog.records)


def test_verbose_survives_logging_before_main_configures_it(monkeypatch):
    # Anything logged before main() configures logging implicitly sets the
    # root logger up, which used to leave --verbose without effect.
    log.info("a stray log line before main() configures logging")

    monkeypatch.setattr(sys, "argv", ["hugepages", "--verbose"])
    with pytest.raises(SystemExit):
        hugepages.main()
    assert log.getLogger().level == log.DEBUG


def test_parser_leaves_the_size_to_the_platform(monkeypatch):
    # No fixed choices and no default: setup_pages() resolves and validates.
    monkeypatch.setattr(sys, "argv", ["hugepages", "setup", "--count", "1"])
    assert hugepages.parse_args().size is None

    monkeypatch.setattr(sys, "argv", ["hugepages", "setup", "--count", "1", "--size", "8192"])
    assert hugepages.parse_args().size == 8192


def _fake_system(monkeypatch, name):
    monkeypatch.setattr(hugepages.platform, "system", lambda: name)


def test_get_platform_dispatch(monkeypatch):
    _fake_system(monkeypatch, "Linux")
    assert isinstance(hugepages.get_platform(), hugepages.Linux)


def test_main_exits_enosys_on_an_unsupported_system(monkeypatch, caplog):
    _fake_system(monkeypatch, "Darwin")
    monkeypatch.setattr(sys, "argv", ["hugepages", "info"])
    with pytest.raises(SystemExit) as excinfo:
        hugepages.main()
    assert excinfo.value.code == errno.ENOSYS
    assert "Darwin" in caplog.text
