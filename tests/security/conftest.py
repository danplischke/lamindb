"""Shared fixtures for security tests.

These tests target permission bypass, data integrity, collaboration,
fuzzing, and concurrency attack surfaces in lamindb.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import lamindb_setup as ln_setup
import pytest
from lamin_utils._logger import logger

# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------

SECURITY_STORAGE = "./test_security_storage"


# ---------------------------------------------------------------------------
# Session-level instance setup / teardown
# ---------------------------------------------------------------------------


def pytest_sessionstart():
    ln_setup.init(
        storage=SECURITY_STORAGE,
        name="lamindb-security-tests",
    )
    os.environ["LAMIN_TESTING"] = "true"
    ln_setup._TESTING = True


def pytest_sessionfinish(session: pytest.Session):
    logger.set_verbosity(1)
    if Path(SECURITY_STORAGE).exists():
        shutil.rmtree(SECURITY_STORAGE)
    ln_setup.delete("lamindb-security-tests", force=True)


# ---------------------------------------------------------------------------
# Reusable fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ccaplog(caplog) -> pytest.LogCaptureFixture:
    """Attach caplog handler to the custom lamin logger."""
    logger.addHandler(caplog.handler)
    yield caplog
    logger.removeHandler(caplog.handler)


@pytest.fixture
def tmp_dir(tmp_path):
    """Provide a clean temporary directory for each test."""
    yield tmp_path
    # tmp_path is auto-cleaned by pytest


@pytest.fixture
def sample_file(tmp_path):
    """Create a small temporary CSV file and return its path."""
    p = tmp_path / "sample.csv"
    p.write_text("id,value\n1,hello\n2,world\n")
    return p


@pytest.fixture
def sample_file_alt(tmp_path):
    """Create a second CSV file with different content (different hash)."""
    p = tmp_path / "sample_alt.csv"
    p.write_text("id,value\n3,foo\n4,bar\n")
    return p


@pytest.fixture
def sample_file_dup(tmp_path):
    """Create a CSV file with identical content to sample_file (same hash)."""
    p = tmp_path / "sample_dup.csv"
    p.write_text("id,value\n1,hello\n2,world\n")
    return p


@pytest.fixture
def large_file(tmp_path):
    """Create a larger file for stress tests."""
    p = tmp_path / "large.csv"
    lines = ["id,value"] + [f"{i},{i*2}" for i in range(10_000)]
    p.write_text("\n".join(lines))
    return p


@pytest.fixture
def nested_dir(tmp_path):
    """Create a nested directory structure for directory artifact tests."""
    d = tmp_path / "nested"
    d.mkdir()
    (d / "file1.txt").write_text("content1")
    (d / "sub").mkdir()
    (d / "sub" / "file2.txt").write_text("content2")
    return d
