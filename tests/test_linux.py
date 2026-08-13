# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) Simon Andreas Frimann Lund <os@safl.dk>
# Copyright (c) Jaeyoon Choi <j_yoon.choi@samsung.com>
"""Unit tests for the Linux platform (runnable on any platform)."""

import logging as log
import subprocess


from hugepages import hugepages


def _fake_sysfs(monkeypatch, tmp_path, sizes, reserved=0):
    """Build a sysfs hugepages tree and point the Linux platform at it"""

    for size in sizes:
        entry = tmp_path / f"hugepages-{size}kB"
        entry.mkdir()
        (entry / "nr_hugepages").write_text(f"{reserved}\n")
        (entry / "free_hugepages").write_text(f"{reserved}\n")
        (entry / "resv_hugepages").write_text("0\n")
    monkeypatch.setattr(hugepages.Linux, "SYSFS", tmp_path)
    return tmp_path


def test_info_reports_every_size(monkeypatch, tmp_path, capsys):
    _fake_sysfs(monkeypatch, tmp_path, [2048, 1048576], reserved=2)
    hugepages.Linux().info()
    out = capsys.readouterr().out
    assert "Size: 2048kB  Total: 2" in out
    assert "Size: 1048576kB" in out


def test_setup_reserves_pages(monkeypatch, tmp_path):
    root = _fake_sysfs(monkeypatch, tmp_path, [2048])
    hugepages.Linux().setup(2048, 8)
    assert (root / "hugepages-2048kB" / "nr_hugepages").read_text().strip() == "8"


def test_setup_count_zero_releases_the_pool(monkeypatch, tmp_path):
    root = _fake_sysfs(monkeypatch, tmp_path, [2048], reserved=4)
    hugepages.Linux().setup(2048, 0)  # a release must not read as a failure
    assert (root / "hugepages-2048kB" / "nr_hugepages").read_text().strip() == "0"


def test_setup_warns_on_a_partial_reservation(monkeypatch, tmp_path, caplog):
    root = _fake_sysfs(monkeypatch, tmp_path, [2048])
    target = root / "hugepages-2048kB" / "nr_hugepages"
    monkeypatch.setattr(hugepages, "sysfs_write", lambda path, text: target.write_text("3\n"))
    with caplog.at_level(log.WARNING):
        hugepages.Linux().setup(2048, 8)
    assert "Only 3 hugepage(s)" in caplog.text


def test_mount_creates_the_mountpoint_and_mounts_it(monkeypatch, tmp_path, capsys):
    calls = []

    def _run(cmd):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(hugepages, "run", _run)
    target = tmp_path / "huge"
    hugepages.Linux().mount(str(target), "2048")
    assert target.is_dir()
    assert calls == [["mount", "-t", "hugetlbfs", "nodev", str(target), "-o", "pagesize=2048k"]]
    assert "Mounted hugetlbfs" in capsys.readouterr().out


def test_mount_passes_a_hostile_mountpoint_as_one_argument(monkeypatch, tmp_path):
    # The command runs without a shell, so metacharacters stay inert.
    calls = []
    monkeypatch.setattr(
        hugepages,
        "run",
        lambda cmd: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )
    hostile = str(tmp_path / "huge; touch pwned")
    hugepages.Linux().mount(hostile)
    assert calls[0] == ["mount", "-t", "hugetlbfs", "nodev", hostile]
    assert not (tmp_path / "pwned").exists()
