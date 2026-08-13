# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) Jaeyoon Choi <j_yoon.choi@samsung.com>
"""Shared fixtures for the hugepages test-suite."""

import logging as log

import pytest


@pytest.fixture(autouse=True)
def restore_log_level():
    """Undo the root-logger level that main() sets

    Without this the first test that drives main() leaves the root logger
    at its level for every test that follows.
    """

    level = log.getLogger().level
    yield
    log.getLogger().setLevel(level)
