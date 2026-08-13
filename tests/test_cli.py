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


def test_get_platform_dispatch(fake_system):
    fake_system("Linux")
    assert isinstance(hugepages.get_platform(), hugepages.Linux)
    fake_system("FreeBSD")
    assert isinstance(hugepages.get_platform(), hugepages.FreeBSD)


def test_get_platform_rejects_an_unsupported_system(fake_system):
    fake_system("Darwin")
    with pytest.raises(NotImplementedError) as excinfo:
        hugepages.get_platform()
    assert "Darwin" in str(excinfo.value)


def test_main_exits_enosys_on_an_unsupported_system(monkeypatch, caplog, fake_system):
    fake_system("Darwin")
    monkeypatch.setattr(sys, "argv", ["hugepages", "info"])
    with pytest.raises(SystemExit) as excinfo:
        hugepages.main()
    assert excinfo.value.code == errno.ENOSYS
    assert "Darwin" in caplog.text


def test_verbose_survives_logging_before_main_configures_it(monkeypatch, fake_system):
    # Anything logged before main() calls basicConfig() implicitly configures
    # the root logger. --verbose must still take effect afterward.
    monkeypatch.setattr(log.getLogger(), "handlers", [])
    log.info("a stray log line before main() configures logging")

    fake_system("Darwin")
    monkeypatch.setattr(sys, "argv", ["hugepages", "--verbose", "info"])
    with pytest.raises(SystemExit):
        hugepages.main()
    assert log.getLogger().level == log.DEBUG


def test_main_turns_a_platform_error_into_lines_and_an_exit_code(monkeypatch, caplog, fake_system):
    class _Failing:
        def setup(self, size, count):
            raise OSError(errno.EINVAL, "first line\nsecond line")

    fake_system("Linux")
    monkeypatch.setattr(hugepages, "get_platform", lambda: _Failing())
    monkeypatch.setattr(sys, "argv", ["hugepages", "setup", "--count", "1"])
    with pytest.raises(SystemExit) as excinfo:
        hugepages.main()
    assert excinfo.value.code == errno.EINVAL
    # One record per line, so no traceback reaches the user.
    assert [record.message for record in caplog.records] == ["first line", "second line"]


def test_run_reports_a_missing_binary_as_a_failed_command():
    # _loaded() and friends rely on a returncode, never on an exception.
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


def test_run_pins_the_message_locale():
    # Failure classification matches English strerror text ("not permitted").
    result = hugepages.run([sys.executable, "-c", "import os; print(os.environ['LC_ALL'])"])
    assert result.stdout.strip() == "C"
