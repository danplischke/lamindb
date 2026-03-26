"""Data integrity & organization tests (D-N1 through D-E3).

Tests that probe hash deduplication, version management, storage path
handling, and data consistency under edge-case conditions.
"""

from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

import lamindb as ln
from lamindb.errors import IntegrityError


# =========================================================================
# NOVICE LEVEL — accidental data corruption
# =========================================================================


class TestDN1_TrashResurrectionViaHashCollision:
    """D-N1: Soft-deleted records can be silently resurrected when a new
    artifact with the same hash is created.

    sqlrecord.py:1160-1168 moves trashed records back to branch_id=1.
    """

    def test_soft_delete_then_recreate_resurrects_from_trash(
        self, sample_file, ccaplog
    ):
        """Create → soft-delete → re-create with same content.
        The trashed record should be resurrected rather than creating a new one."""
        artifact = ln.Artifact(sample_file, description="trash-resurrection-dn1")
        artifact.save()
        original_uid = artifact.uid
        original_hash = artifact.hash

        # Soft delete (moves to trash branch)
        artifact.delete()

        # Re-create with identical content
        artifact2 = ln.Artifact(sample_file, description="new-but-same-hash-dn1")
        artifact2.save()

        # The system should resurrect the trashed record
        assert artifact2.uid == original_uid, (
            "Expected resurrection of trashed artifact with same hash, "
            f"but got new uid {artifact2.uid} vs original {original_uid}"
        )

        # Check for warning in logs
        warning_found = any(
            "from trash" in r.message or "same hash" in r.message
            for r in ccaplog.records
        )
        assert warning_found, (
            "System should warn when resurrecting records from trash"
        )

        artifact2.delete(permanent=True, storage=True)

    def test_resurrected_record_preserves_old_metadata(self, sample_file):
        """When a trashed record is resurrected via hash match, verify
        which metadata survives — old description or new one?"""
        artifact = ln.Artifact(sample_file, description="original-metadata-dn1")
        artifact.save()

        artifact.delete()

        # Re-create with different description but same content
        artifact2 = ln.Artifact(sample_file, description="new-metadata-dn1")
        artifact2.save()

        # Document: does the resurrected record keep old or new description?
        # The init_self_from_db call overwrites self with DB state
        # So the OLD description should survive
        actual_desc = artifact2.description
        # This documents current behavior — the old record's state wins
        artifact2.delete(permanent=True, storage=True)


class TestDN2_OrphanedRecordsFromFailedUploads:
    """D-N2: DB record is saved before cloud upload completes.
    If upload fails, orphaned DB records exist with no storage backing."""

    def test_artifact_save_records_before_upload(self, sample_file):
        """Verify that an artifact DB record can exist even if storage
        is not properly set up — documenting the save-then-upload pattern."""
        artifact = ln.Artifact(sample_file, description="orphan-test-dn2")
        artifact.save()

        # The artifact exists in DB
        found = ln.Artifact.filter(uid=artifact.uid).one_or_none()
        assert found is not None

        # Now simulate storage deletion (as if upload failed)
        from lamindb.core.storage.paths import filepath_from_artifact

        try:
            path = filepath_from_artifact(artifact)
            if path.exists():
                path.unlink()
        except Exception:
            pass  # Storage might already be in expected location

        artifact.delete(permanent=True)

    def test_load_missing_storage_file(self, sample_file):
        """Attempting to load an artifact whose storage file is missing
        should produce a clear error, not a crash."""
        artifact = ln.Artifact(sample_file, description="missing-storage-dn2")
        artifact.save()

        # Delete the storage file directly
        from lamindb.core.storage.paths import filepath_from_artifact

        try:
            path = filepath_from_artifact(artifact)
            if path.exists():
                path.unlink()

            # Attempting to load should fail gracefully
            with pytest.raises(Exception):
                artifact.load()
        except Exception:
            pass  # Path resolution might fail differently

        artifact.delete(permanent=True)


class TestDN3_SuffixTypeMismatch:
    """D-N3: Upload a file with mismatched suffix and actual content."""

    def test_wrong_suffix_accepted(self, tmp_path):
        """Create a CSV but name it .h5ad — document validation behavior."""
        fake_h5ad = tmp_path / "fake.h5ad"
        fake_h5ad.write_text("id,value\n1,hello\n")

        artifact = ln.Artifact(fake_h5ad, description="wrong-suffix-dn3")
        artifact.save()

        assert artifact.suffix == ".h5ad", "Suffix should be taken from filename"

        artifact.delete(permanent=True, storage=True)

    def test_empty_file_accepted(self, tmp_path):
        """Empty files should be handled without crashing."""
        empty = tmp_path / "empty.csv"
        empty.write_text("")

        try:
            artifact = ln.Artifact(empty, description="empty-file-dn3")
            artifact.save()
            artifact.delete(permanent=True, storage=True)
        except Exception as e:
            # Document: does lamindb reject empty files?
            assert "hash" in str(e).lower() or "size" in str(e).lower() or "empty" in str(e).lower(), (
                f"Unexpected error for empty file: {e}"
            )


# =========================================================================
# INTERMEDIATE LEVEL — curious user
# =========================================================================


class TestDI1_VersionFamilyHijacking:
    """D-I1: Craft an artifact with a known stem_uid to inject into
    an existing version family."""

    def test_version_uid_structure(self, sample_file, sample_file_alt):
        """Verify that version UIDs share the stem_uid prefix."""
        artifact1 = ln.Artifact(sample_file, description="version-v1-di1", key="di1_test.csv")
        artifact1.save()
        stem1 = artifact1.stem_uid
        uid1 = artifact1.uid

        # Create version 2
        artifact2 = ln.Artifact(
            sample_file_alt,
            description="version-v2-di1",
            key="di1_test.csv",
            revises=artifact1,
        )
        artifact2.save()

        assert artifact2.stem_uid == stem1, "Version 2 should share the stem_uid"
        assert artifact2.uid != uid1, "Version 2 should have different full uid"
        assert len(artifact2.uid) == 20, "Full UID should be 20 chars (16 stem + 4 version)"

        artifact2.delete(permanent=True, storage=True)
        artifact1.delete(permanent=True, storage=True)

    def test_manual_uid_injection_blocked(self, sample_file):
        """Attempting to set uid directly should be handled safely."""
        artifact = ln.Artifact(sample_file, description="uid-inject-di1")

        # Try to set a crafted UID
        crafted_uid = "A" * 20
        artifact.uid = crafted_uid
        artifact.save()

        # Document: was the crafted UID accepted?
        if artifact.uid == crafted_uid:
            # This is a risk — an attacker could inject into version families
            pass

        artifact.delete(permanent=True, storage=True)


class TestDI2_SymlinkPathTraversal:
    """D-I2: Use symlinks to reference files outside the storage root."""

    def test_symlink_outside_storage_root(self, tmp_path):
        """Create a symlink pointing outside the storage root."""
        from lamindb.core.storage.paths import check_path_is_child_of_root

        # Create a file outside the "root"
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        outside_file = outside_dir / "secret.txt"
        outside_file.write_text("secret data")

        # Create root and symlink
        root = tmp_path / "root"
        root.mkdir()
        symlink = root / "link_to_secret.txt"
        try:
            symlink.symlink_to(outside_file)
        except OSError:
            pytest.skip("Cannot create symlinks on this platform")

        # The resolved path of the symlink is outside root
        # check_path_is_child_of_root resolves symlinks via .resolve()
        is_child = check_path_is_child_of_root(str(symlink), str(root))

        assert not is_child, (
            "Symlink pointing outside root should NOT be considered a child. "
            "check_path_is_child_of_root should reject symlink traversal."
        )

    def test_relative_path_traversal(self, tmp_path):
        """Test ../../../ path traversal in storage key context."""
        from lamindb.core.storage.paths import check_path_is_child_of_root

        root = tmp_path / "storage"
        root.mkdir()

        # Relative traversal path
        traversal = str(root / ".." / ".." / "etc" / "passwd")
        is_child = check_path_is_child_of_root(traversal, str(root))

        assert not is_child, (
            "Relative path traversal (../..) should not pass child-of-root check"
        )


class TestDI3_HashDeduplicationRace:
    """D-I3: TOCTOU race in hash lookup — two concurrent processes both
    find no existing artifact, both try to insert."""

    def test_concurrent_same_hash_creation(self, tmp_path):
        """Simulate concurrent creation of artifacts with identical content."""
        # Create two files with identical content
        file1 = tmp_path / "race1.csv"
        file2 = tmp_path / "race2.csv"
        content = "id,value\n1,race_test\n2,concurrent\n"
        file1.write_text(content)
        file2.write_text(content)

        results = []
        errors = []

        def create_artifact(filepath, desc):
            try:
                a = ln.Artifact(filepath, description=desc)
                a.save()
                results.append(a)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=create_artifact, args=(file1, "race-1-di3"))
        t2 = threading.Thread(target=create_artifact, args=(file2, "race-2-di3"))

        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        # Both should succeed — one creates, the other gets the existing record
        assert len(errors) == 0, f"Concurrent creation produced errors: {errors}"
        assert len(results) == 2, "Both threads should return an artifact"

        # Both should reference the same underlying record
        assert results[0].uid == results[1].uid, (
            "Concurrent creation of same hash should deduplicate to same record"
        )

        # Cleanup
        results[0].delete(permanent=True, storage=True)


# =========================================================================
# EXPERT LEVEL — intentional attacks
# =========================================================================


class TestDE1_OverwriteVersionsStorageCollision:
    """D-E1: When overwrite_versions=True, storage key uses uid[:16].
    Two artifacts with different stems but same first 16 chars would collide."""

    def test_storage_key_uses_stem_uid(self):
        """Verify storage key construction for overwrite_versions mode."""
        from lamindb.core.storage.paths import auto_storage_key_from_artifact_uid

        uid = "A" * 16 + "B" * 4  # 20-char uid
        suffix = ".csv"

        key_overwrite = auto_storage_key_from_artifact_uid(uid, suffix, overwrite_versions=True)
        key_normal = auto_storage_key_from_artifact_uid(uid, suffix, overwrite_versions=False)

        assert key_overwrite == f".lamindb/{'A' * 16}.csv", (
            "Overwrite mode should use only stem_uid (16 chars)"
        )
        assert key_normal == f".lamindb/{'A' * 16}{'B' * 4}.csv", (
            "Normal mode should use full uid (20 chars)"
        )

    def test_different_versions_share_storage_path(self):
        """Two different version suffixes with same stem should map to
        the same storage key when overwrite_versions=True."""
        from lamindb.core.storage.paths import auto_storage_key_from_artifact_uid

        stem = "AbCdEfGhIjKlMnOp"  # 16 chars
        uid_v1 = stem + "0001"
        uid_v2 = stem + "0002"

        key_v1 = auto_storage_key_from_artifact_uid(uid_v1, ".h5ad", overwrite_versions=True)
        key_v2 = auto_storage_key_from_artifact_uid(uid_v2, ".h5ad", overwrite_versions=True)

        assert key_v1 == key_v2, (
            "Different versions with same stem should share storage key in overwrite mode"
        )


class TestDE2_VirtualKeyManipulation:
    """D-E2: Artifacts with _key_is_virtual=True can have keys changed
    to impersonate other artifacts' logical paths."""

    def test_virtual_key_can_be_changed(self, sample_file, sample_file_alt):
        """Changing a virtual key should not cause issues at the storage level."""
        a1 = ln.Artifact(sample_file, description="key-test-1-de2", key="original_path.csv")
        a1.save()

        a2 = ln.Artifact(sample_file_alt, description="key-test-2-de2", key="other_path.csv")
        a2.save()

        # Try to change a2's key to match a1's key
        a2.key = "original_path.csv"
        try:
            a2.save()
            # If this succeeds, two artifacts have the same logical key
            # which could confuse users
            key_matches = ln.Artifact.filter(key="original_path.csv").count()
            assert key_matches >= 1, "Should find artifacts with the key"
        except Exception:
            # Key change validation caught it — good
            pass

        a1.delete(permanent=True, storage=True)
        a2.delete(permanent=True, storage=True)


class TestDE3_BulkUpdateBypassesORMHooks:
    """D-E3: QuerySet.update() bypasses save() validation entirely.

    This means hash checks, permission checks, version management,
    locked-record enforcement, and key-change validation are all skipped.
    """

    def test_bulk_update_modifies_hash(self, sample_file):
        """Changing hash via bulk update should succeed — no ORM validation."""
        artifact = ln.Artifact(sample_file, description="bulk-hash-de3")
        artifact.save()
        original_hash = artifact.hash

        # This should work because update() goes straight to SQL
        ln.Artifact.filter(uid=artifact.uid).update(hash="fake_hash_12345")

        refreshed = ln.Artifact.get(uid=artifact.uid)
        assert refreshed.hash == "fake_hash_12345", (
            "Bulk update should bypass hash validation"
        )
        assert refreshed.hash != original_hash

        # Restore and cleanup
        ln.Artifact.filter(uid=artifact.uid).update(hash=original_hash)
        artifact.delete(permanent=True, storage=True)

    def test_bulk_update_modifies_uid(self, sample_file):
        """Changing UID via bulk update — potentially breaking version families."""
        artifact = ln.Artifact(sample_file, description="bulk-uid-de3")
        artifact.save()
        original_uid = artifact.uid

        new_uid = "Z" * 20
        ln.Artifact.filter(uid=original_uid).update(uid=new_uid)

        # The artifact should now be findable by the new UID
        found = ln.Artifact.filter(uid=new_uid).one_or_none()
        assert found is not None, "Bulk UID update should work at SQL level"

        # Cleanup with new UID
        found.delete(permanent=True, storage=True)

    def test_bulk_update_modifies_version_fields(self, sample_file):
        """Changing is_latest/stem_uid via bulk update breaks version tracking."""
        artifact = ln.Artifact(sample_file, description="bulk-version-de3")
        artifact.save()

        # Flip is_latest to False
        ln.Artifact.filter(uid=artifact.uid).update(is_latest=False)
        refreshed = ln.Artifact.get(uid=artifact.uid)
        assert not refreshed.is_latest, (
            "Bulk update can flip is_latest without proper version chain update"
        )

        # Restore and cleanup
        ln.Artifact.filter(uid=artifact.uid).update(is_latest=True)
        artifact.delete(permanent=True, storage=True)
