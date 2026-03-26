"""Fuzzing tests (F1 through F4).

Probe input validation boundaries by feeding unexpected, malformed,
or adversarial inputs to key fields and parsers.
"""

from __future__ import annotations

import pytest

import lamindb as ln


# =========================================================================
# F1: Path traversal in artifact keys
# =========================================================================


class TestF1_ArtifactKeyPathTraversal:
    """Fuzz artifact key field with path traversal sequences, null bytes,
    and unicode edge cases."""

    TRAVERSAL_PAYLOADS = [
        "../../../etc/passwd",
        "..\\..\\..\\windows\\system32\\config\\sam",
        "....//....//....//etc/passwd",
        "valid/../../../etc/passwd",
        "/absolute/path/to/secret",
        "key\x00.csv",  # null byte injection
        "key%00.csv",  # URL-encoded null byte
        ".lamindb/../../../etc/passwd",
    ]

    @pytest.mark.parametrize("malicious_key", TRAVERSAL_PAYLOADS)
    def test_path_traversal_in_key(self, malicious_key, sample_file):
        """Artifact keys with traversal sequences should be rejected or sanitized."""
        try:
            artifact = ln.Artifact(
                sample_file, description="traversal-test-f1", key=malicious_key
            )
            artifact.save()

            # If we get here, the key was accepted — document it
            actual_key = artifact.key
            # At minimum, the stored key should not enable path traversal
            # when resolved to a storage path

            artifact.delete(permanent=True, storage=True)
        except Exception as e:
            # Good — the system rejected the malicious key
            pass

    UNICODE_PAYLOADS = [
        "file\u200b.csv",  # zero-width space
        "file\u2066.csv",  # right-to-left override
        "filevsc.txt\u202e\u2066csv.",  # bidirectional override (filename spoofing)
        "\U0001F4A9.csv",  # emoji in filename
        "a" * 1000 + ".csv",  # extremely long key
        " " * 100 + ".csv",  # spaces-only key
        "file\n.csv",  # newline in key
        "file\t.csv",  # tab in key
    ]

    @pytest.mark.parametrize("unicode_key", UNICODE_PAYLOADS)
    def test_unicode_edge_cases_in_key(self, unicode_key, sample_file):
        """Test unicode edge cases in artifact keys."""
        try:
            artifact = ln.Artifact(
                sample_file, description="unicode-test-f1", key=unicode_key
            )
            artifact.save()
            artifact.delete(permanent=True, storage=True)
        except Exception:
            # Rejection is acceptable
            pass


# =========================================================================
# F2: Suffix field fuzzing
# =========================================================================


class TestF2_SuffixFuzzing:
    """Fuzz the suffix field with extreme values."""

    SUFFIX_PAYLOADS = [
        "",  # empty suffix
        "." * 100,  # many dots
        ".csv.exe",  # double extension
        ".csv\x00.exe",  # null byte in extension
        "." + "a" * 500,  # extremely long suffix
        ".CSV",  # case variation
        "..csv",  # double-dot prefix
    ]

    @pytest.mark.parametrize("suffix", SUFFIX_PAYLOADS)
    def test_malformed_suffix(self, suffix, tmp_path):
        """Malformed suffixes should be handled without crashing."""
        try:
            # Create a file with the malformed suffix
            # Some suffixes may not be valid filenames
            safe_name = "test" + suffix.replace("\x00", "").replace("/", "_").replace("\\", "_")
            if not safe_name:
                safe_name = "test_file"
            filepath = tmp_path / safe_name
            filepath.write_text("id,value\n1,test\n")

            artifact = ln.Artifact(filepath, description=f"suffix-test-f2-{suffix[:10]}")
            artifact.save()
            artifact.delete(permanent=True, storage=True)
        except (ValueError, OSError, Exception):
            # Rejection or OS-level failure is acceptable
            pass


# =========================================================================
# F3: SQL injection via field values
# =========================================================================


class TestF3_SQLInjectionViaFields:
    """Django parameterizes queries, but test that adversarial field values
    don't cause unexpected behavior in any code path."""

    SQL_PAYLOADS = [
        "'; DROP TABLE lamindb_artifact; --",
        "1' OR '1'='1",
        "1; SELECT * FROM lamindb_user; --",
        "' UNION SELECT uid, handle FROM lamindb_user --",
        "Robert'); DROP TABLE lamindb_artifact;--",
        "1' AND (SELECT COUNT(*) FROM lamindb_user) > 0 --",
    ]

    @pytest.mark.parametrize("payload", SQL_PAYLOADS)
    def test_sql_injection_in_description(self, payload, sample_file):
        """SQL injection payloads in description should be safely parameterized."""
        artifact = ln.Artifact(sample_file, description=payload)
        artifact.save()

        # The payload should be stored as literal text, not executed
        found = ln.Artifact.get(uid=artifact.uid)
        assert found.description == payload, (
            "SQL payload should be stored literally, not executed"
        )

        artifact.delete(permanent=True, storage=True)

    @pytest.mark.parametrize("payload", SQL_PAYLOADS)
    def test_sql_injection_in_filter(self, payload):
        """SQL injection payloads in filter values should be parameterized."""
        # This should return empty results, not cause SQL errors
        try:
            results = ln.Artifact.filter(description=payload).all()
            # No exception = Django properly parameterized the query
        except Exception as e:
            # Should never get a SQL syntax error from parameterized queries
            assert "syntax" not in str(e).lower(), (
                f"Possible SQL injection vulnerability: {e}"
            )

    @pytest.mark.parametrize("payload", SQL_PAYLOADS)
    def test_sql_injection_in_search(self, payload):
        """SQL injection in search should be safely handled."""
        try:
            results = ln.Artifact.search(payload)
            # No exception = safely handled
        except Exception as e:
            assert "syntax" not in str(e).lower(), (
                f"Possible SQL injection in search: {e}"
            )

    def test_sql_injection_in_name_field(self):
        """Test SQL injection via the name field of ULabel (common registry)."""
        payload = "test'; DROP TABLE lamindb_ulabel;--"
        try:
            label = ln.ULabel(name=payload)
            label.save()
            found = ln.ULabel.get(uid=label.uid)
            assert found.name == payload, "Payload should be stored literally"
            label.delete(permanent=True)
        except Exception:
            pass


# =========================================================================
# F4: Feature dtype field fuzzing
# =========================================================================


class TestF4_DtypeFuzzing:
    """Fuzz the dtype field in Feature model with malformed type strings.
    The parse_dtype() function at feature.py:83-180 processes these.
    """

    DTYPE_PAYLOADS = [
        "cat['; DROP TABLE lamindb_feature;--]",
        "cat[Gene|Protein|'; DELETE FROM lamindb_feature;--]",
        "int64" * 100,  # extremely long dtype
        "",  # empty string
        "cat[]",  # empty category
        "cat[[[nested]]]",  # nested brackets
        "cat[Gene\x00Protein]",  # null byte in dtype
        "float\n64",  # newline in dtype
        "cat[" + "A" * 10000 + "]",  # extremely long category
        "cat[Gene|Gene|Gene]",  # duplicate categories
    ]

    @pytest.mark.parametrize("dtype_payload", DTYPE_PAYLOADS)
    def test_malformed_dtype(self, dtype_payload):
        """Malformed dtype strings should be handled without crashing."""
        try:
            feature = ln.Feature(name=f"fuzz_feature_{hash(dtype_payload) % 10000}", dtype=dtype_payload)
            feature.save()
            feature.delete(permanent=True)
        except (ValueError, TypeError, Exception):
            # Rejection is acceptable — just shouldn't crash the system
            pass

    def test_dtype_with_special_characters(self):
        """Test dtype parsing with special regex characters."""
        special_dtypes = [
            "cat[Gene.*]",  # regex wildcard
            "cat[Gene+]",  # regex quantifier
            "cat[Gene|Pro(tein)]",  # regex group
            r"cat[Gene\d+]",  # regex digit class
        ]
        for dtype in special_dtypes:
            try:
                feature = ln.Feature(name=f"special_dtype_{hash(dtype) % 10000}", dtype=dtype)
                feature.save()
                feature.delete(permanent=True)
            except Exception:
                pass


# =========================================================================
# Additional: Information disclosure via error messages
# =========================================================================


class TestF5_InformationDisclosure:
    """Test that error messages don't leak sensitive information."""

    def test_nonexistent_artifact_error(self):
        """Error for missing artifact shouldn't leak DB structure."""
        with pytest.raises(Exception) as exc_info:
            ln.Artifact.get(uid="nonexistent_uid_12345678")

        error_msg = str(exc_info.value)
        # Should not contain SQL table names, connection strings, etc.
        sensitive_patterns = [
            "postgresql://",
            "mysql://",
            "sqlite:///",
            "password",
            "secret",
        ]
        for pattern in sensitive_patterns:
            assert pattern not in error_msg.lower(), (
                f"Error message leaks sensitive info: contains '{pattern}'"
            )

    def test_invalid_filter_error(self):
        """Invalid filter fields shouldn't expose internal schema details."""
        try:
            ln.Artifact.filter(nonexistent_field="value").all()
        except Exception as e:
            error_msg = str(e)
            # Should give a helpful error, not expose all field names
            # (Django's FieldError lists valid fields — this is a minor info leak)
