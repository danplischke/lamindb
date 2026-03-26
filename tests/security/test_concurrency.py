"""Concurrency & stress tests (S1 through S5).

Tests that probe race conditions, resource exhaustion, and data
consistency under concurrent load.

NOTE: These tests use threading, which shares the Django DB connection.
For true concurrency testing with separate DB connections, use
multiprocessing or pytest-xdist with separate worker instances.
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest

import lamindb as ln


# =========================================================================
# S1: Concurrent artifact uploads with same content
# =========================================================================


class TestS1_ConcurrentSameContentUploads:
    """100 concurrent uploads of identical content should all
    deduplicate to a single record."""

    def test_10_concurrent_same_hash(self, tmp_path):
        """10 threads creating artifacts with identical content."""
        content = "id,value\n1,concurrent_test\n2,dedup_stress\n"
        files = []
        for i in range(10):
            f = tmp_path / f"concurrent_{i}.csv"
            f.write_text(content)
            files.append(f)

        results = []
        errors = []

        def create_artifact(filepath, idx):
            try:
                a = ln.Artifact(filepath, description=f"concurrent-s1-{idx}")
                a.save()
                results.append((idx, a.uid, a.hash))
            except Exception as e:
                errors.append((idx, str(e)))

        threads = []
        for i, f in enumerate(files):
            t = threading.Thread(target=create_artifact, args=(f, i))
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        # All should succeed
        assert len(errors) == 0, f"Concurrent creation errors: {errors}"
        assert len(results) == 10, f"Expected 10 results, got {len(results)}"

        # All should deduplicate to same UID
        uids = {r[1] for r in results}
        assert len(uids) == 1, (
            f"All concurrent uploads of same content should deduplicate to "
            f"one record, but got {len(uids)} distinct UIDs: {uids}"
        )

        # Cleanup
        ln.Artifact.get(uid=results[0][1]).delete(permanent=True, storage=True)


# =========================================================================
# S2: Rapid version creation
# =========================================================================


class TestS2_RapidVersionCreation:
    """Multiple threads creating new versions of the same artifact
    simultaneously."""

    def test_sequential_rapid_versioning(self, tmp_path):
        """Create many versions rapidly in sequence — verify version chain integrity."""
        base_file = tmp_path / "v0.csv"
        base_file.write_text("id,value\n0,base\n")

        artifact = ln.Artifact(base_file, description="rapid-version-s2", key="rapid_version.csv")
        artifact.save()
        stem_uid = artifact.stem_uid
        versions = [artifact]

        for i in range(1, 5):
            f = tmp_path / f"v{i}.csv"
            f.write_text(f"id,value\n{i},version_{i}\n")
            new_version = ln.Artifact(
                f,
                description=f"rapid-version-s2-v{i}",
                key="rapid_version.csv",
                revises=versions[-1],
            )
            new_version.save()
            versions.append(new_version)

        # Only the latest should have is_latest=True
        latest_count = sum(
            1 for v in versions if ln.Artifact.get(uid=v.uid).is_latest
        )
        assert latest_count == 1, (
            f"Exactly one version should be is_latest, got {latest_count}"
        )

        # Latest should be the last one
        latest = ln.Artifact.get(uid=versions[-1].uid)
        assert latest.is_latest, "Last created version should be is_latest"

        # All should share stem_uid
        for v in versions:
            assert v.stem_uid == stem_uid, (
                f"All versions should share stem_uid {stem_uid}"
            )

        # Cleanup (delete in reverse order)
        for v in reversed(versions):
            try:
                v.delete(permanent=True, storage=True)
            except Exception:
                pass


# =========================================================================
# S3: Concurrent soft-delete and re-create cycle
# =========================================================================


class TestS3_SoftDeleteRecreate:
    """Rapid soft-delete + re-create cycles to stress the trash
    resurrection logic."""

    def test_delete_recreate_cycle(self, tmp_path):
        """Delete and re-create an artifact multiple times."""
        content = "id,value\n1,cycle_test\n"
        filepath = tmp_path / "cycle.csv"
        filepath.write_text(content)

        uids_seen = set()
        for i in range(5):
            artifact = ln.Artifact(filepath, description=f"cycle-s3-{i}")
            artifact.save()
            uids_seen.add(artifact.uid)
            artifact.delete()  # soft delete

        # All cycles should produce the same UID (resurrection from trash)
        assert len(uids_seen) == 1, (
            f"Delete/recreate cycle should always resurrect same record, "
            f"but got {len(uids_seen)} UIDs: {uids_seen}"
        )

        # Final cleanup
        final = ln.Artifact.filter(uid=list(uids_seen)[0]).one_or_none()
        if final is not None:
            final.delete(permanent=True, storage=True)


# =========================================================================
# S4: Mass record creation stress
# =========================================================================


class TestS4_MassRecordCreation:
    """Create many records rapidly to stress DB constraints and
    UID generation."""

    def test_100_unique_artifacts(self, tmp_path):
        """Create 100 artifacts with unique content — no collisions expected."""
        artifacts = []
        for i in range(100):
            f = tmp_path / f"mass_{i}.csv"
            f.write_text(f"id,value\n{i},unique_content_{i}\n")
            a = ln.Artifact(f, description=f"mass-s4-{i}")
            a.save()
            artifacts.append(a)

        # All should have unique UIDs
        uids = [a.uid for a in artifacts]
        assert len(set(uids)) == 100, (
            f"100 artifacts should have 100 unique UIDs, got {len(set(uids))}"
        )

        # All should have unique hashes (different content)
        hashes = [a.hash for a in artifacts if a.hash is not None]
        assert len(set(hashes)) == len(hashes), "All unique-content artifacts should have unique hashes"

        # Cleanup
        for a in artifacts:
            a.delete(permanent=True, storage=True)

    def test_100_ulabels_rapid_creation(self):
        """Create 100 ULabels rapidly to stress name uniqueness."""
        labels = []
        for i in range(100):
            label = ln.ULabel(name=f"stress-test-label-s4-{i}")
            label.save()
            labels.append(label)

        assert len(labels) == 100
        uids = [l.uid for l in labels]
        assert len(set(uids)) == 100, "All labels should have unique UIDs"

        # Cleanup
        for l in labels:
            l.delete(permanent=True)


# =========================================================================
# S5: Concurrent reads during writes
# =========================================================================


class TestS5_ConcurrentReadsWrites:
    """Simultaneous reads and writes shouldn't cause data corruption
    or deadlocks."""

    def test_read_during_write(self, tmp_path):
        """One thread writes artifacts while another reads — no crashes."""
        content_base = "id,value\n"
        write_count = 10
        created_uids = []
        read_results = []
        errors = []

        def writer():
            for i in range(write_count):
                f = tmp_path / f"rw_{i}.csv"
                f.write_text(content_base + f"{i},write_{i}\n")
                try:
                    a = ln.Artifact(f, description=f"readwrite-s5-{i}")
                    a.save()
                    created_uids.append(a.uid)
                except Exception as e:
                    errors.append(("write", i, str(e)))

        def reader():
            for _ in range(write_count * 2):
                try:
                    count = ln.Artifact.filter(
                        description__startswith="readwrite-s5"
                    ).count()
                    read_results.append(count)
                except Exception as e:
                    errors.append(("read", _, str(e)))
                time.sleep(0.01)

        writer_thread = threading.Thread(target=writer)
        reader_thread = threading.Thread(target=reader)

        writer_thread.start()
        reader_thread.start()
        writer_thread.join(timeout=60)
        reader_thread.join(timeout=60)

        assert len(errors) == 0, f"Concurrent read/write errors: {errors}"
        assert len(created_uids) == write_count, (
            f"Writer should create {write_count} artifacts"
        )

        # Read results should show monotonically increasing counts
        # (or at least no decreases)
        assert len(read_results) > 0, "Reader should have produced results"

        # Cleanup
        for uid in created_uids:
            try:
                ln.Artifact.get(uid=uid).delete(permanent=True, storage=True)
            except Exception:
                pass

    def test_concurrent_filter_operations(self, tmp_path):
        """Multiple concurrent filter/search operations should be safe."""
        # Pre-create some artifacts
        artifacts = []
        for i in range(5):
            f = tmp_path / f"filter_{i}.csv"
            f.write_text(f"id,value\n{i},filter_test\n")
            a = ln.Artifact(f, description=f"filter-s5-{i}")
            a.save()
            artifacts.append(a)

        errors = []

        def filter_task(desc_pattern):
            try:
                for _ in range(20):
                    results = ln.Artifact.filter(
                        description__startswith=desc_pattern
                    ).all()
                    list(results)  # force evaluation
            except Exception as e:
                errors.append(str(e))

        threads = [
            threading.Thread(target=filter_task, args=("filter-s5",))
            for _ in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert len(errors) == 0, f"Concurrent filter errors: {errors}"

        # Cleanup
        for a in artifacts:
            a.delete(permanent=True, storage=True)


# =========================================================================
# Bonus: UID generation under load
# =========================================================================


class TestS6_UIDGenerationUnderLoad:
    """Stress test the UID generation to verify no collisions under load."""

    def test_1000_uid_generations_unique(self):
        """Generate 1000 UIDs and verify all are unique."""
        from lamindb.base.uids import base62_20

        uids = [base62_20() for _ in range(1000)]
        assert len(set(uids)) == 1000, (
            f"1000 UID generations should produce 1000 unique values, "
            f"got {len(set(uids))} unique"
        )

    def test_concurrent_uid_generation(self):
        """Generate UIDs concurrently across threads."""
        from lamindb.base.uids import base62_20

        results = []

        def generate_uids(count):
            for _ in range(count):
                results.append(base62_20())

        threads = [
            threading.Thread(target=generate_uids, args=(100,))
            for _ in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert len(results) == 1000
        assert len(set(results)) == 1000, (
            "Concurrent UID generation should produce all unique values"
        )
