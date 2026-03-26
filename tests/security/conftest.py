"""Shared fixtures for security tests.

These tests target permission bypass, data integrity, collaboration,
fuzzing, and concurrency attack surfaces in lamindb.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from unittest.mock import patch

import lamindb_setup as ln_setup
import pytest
from lamin_utils._logger import logger

SECURITY_STORAGE = "./test_security_storage"


def _init_instance_offline():
    """Initialize a local lamindb instance without hub connectivity.

    Patches hub calls so that tests can run in sandboxed / offline
    environments.
    """
    os.environ["LAMIN_TESTING"] = "true"
    ln_setup._TESTING = True

    # Clean up leftover storage from previous interrupted runs
    storage_path = Path(SECURITY_STORAGE)
    if storage_path.exists():
        shutil.rmtree(storage_path, ignore_errors=True)

    # Patch hub storage check to pretend no hub record exists.
    # This lets init() proceed with a purely local SQLite instance.
    with patch(
        "lamindb_setup.core._hub_core.init_storage_hub",
        return_value="created",
    ), patch(
        "lamindb_setup.core._hub_core.init_instance_hub",
        return_value=None,
    ):
        ln_setup.init(
            storage=SECURITY_STORAGE,
            name="lamindb-security-tests",
        )

    # Permanently patch is_on_hub to return False so that no hub calls
    # are made during artifact creation / storage operations.
    from lamindb_setup.core._settings_instance import InstanceSettings

    InstanceSettings.is_on_hub = property(lambda self: False)
    # Also mark on the storage settings
    from lamindb_setup.core._settings_storage import StorageSettings

    StorageSettings.is_on_hub = property(lambda self: False)

    # Silence missing-run warnings globally
    import lamindb as ln

    ln.settings.creation.artifact_silence_missing_run_warning = True


def pytest_sessionstart():
    try:
        _init_instance_offline()
    except Exception as e:
        # If already initialised from a previous interrupted run, just load.
        try:
            ln_setup.connect("lamindb-security-tests")
        except Exception:
            raise RuntimeError(
                f"Failed to init lamindb for security tests: {e}"
            ) from e


def pytest_sessionfinish(session: pytest.Session):
    logger.set_verbosity(1)
    try:
        with patch(
            "lamindb_setup.core._hub_core.delete_instance_hub",
            return_value=None,
        ):
            ln_setup.delete("lamindb-security-tests", force=True)
    except Exception:
        pass
    if Path(SECURITY_STORAGE).exists():
        shutil.rmtree(SECURITY_STORAGE, ignore_errors=True)


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
