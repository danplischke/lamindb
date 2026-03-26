# LaminDB Security Testing Findings

> **Test Suite**: `tests/security/` | **114 passed, 2 skipped** | **Date**: 2026-03-26
>
> Tests run against a local SQLite instance. Findings marked **[PG-ONLY]** require PostgreSQL + RLS for full validation.

---

## Summary

| Severity | Count | Category |
|----------|-------|----------|
| Critical | 3 | Permission bypass, data integrity |
| High | 5 | ORM bypass, collaboration, concurrency |
| Medium | 6 | Audit trail, validation gaps, information flow |
| Low | 4 | Cosmetic, documentation gaps |

---

## Critical Findings

### CRIT-1: `QuerySet.update()` Bypasses ALL ORM-Level Security Hooks

**Tests**: `TestDE3_BulkUpdateBypassesORMHooks` (3 tests, all pass — proving the bypass)

Django's `QuerySet.update()` goes directly to SQL, completely bypassing `save()` which contains:
- Hash integrity checks
- Permission/RLS error handling
- Locked record enforcement
- Version management (`is_latest` transitions)
- Key/name change validation

**Proven**: Tests successfully modified `hash`, `uid`, and `is_latest` via bulk update without any validation.

**Impact**: Any user with DB write access can corrupt data integrity by modifying hashes (breaking deduplication), UIDs (breaking version families), or version flags.

**Location**: `lamindb/models/sqlrecord.py:1085-1233` — all validation lives exclusively in `save()`

**Recommendation**: Add database-level triggers or constraints for critical fields, or override the QuerySet manager to intercept `update()` calls.

---

### CRIT-2: SQLite Instances Have Zero Permission Enforcement

**Tests**: `TestPN1_SQLiteNoAccessControl` (3 tests, all pass)

Spaces on SQLite are purely cosmetic. The system warns during `Space` creation ("does *not* affect access permissions") but:
- All records remain readable regardless of space assignment
- No write restrictions are enforced
- Direct SQL access is completely unrestricted

**Impact**: Users who develop locally with SQLite may have a false sense of security, assuming spaces protect their data when they don't.

**Location**: `lamindb/models/sqlrecord.py:1450-1465`

**Recommendation**: Add prominent documentation warning. Consider adding optional ORM-level space checks for SQLite (even without RLS).

---

### CRIT-3: Direct SQL Bypasses All ORM Controls on SQLite

**Tests**: `TestPE1_DirectDatabaseBypass` (2 tests, both pass — proving the bypass)

Direct SQL queries via Django's `connection.cursor()` bypass all lamindb protections. Both SELECT and UPDATE work without restriction.

**Impact**: Any process with access to the SQLite file can read/modify all data. On PostgreSQL, RLS would block this (if properly configured).

**Location**: No protection layer exists between Django ORM and SQLite.

---

## High Severity Findings

### HIGH-1: Transfer Destroys Audit Trail (Authorship Loss)

**Tests**: `TestCN1_TransferOverwritesAuthorship` (2 tests, both pass)

When records are transferred between instances, `created_by_id` is silently reassigned to the transferring user:

```python
# sqlrecord.py:2118
record.created_by_id = ln_setup.settings.user.id
```

The original creator's identity is permanently lost. There is no field preserving the source authorship.

**Impact**: Audit trails become unreliable after any cross-instance transfer. Regulatory/compliance issues for data provenance.

**Location**: `lamindb/models/sqlrecord.py:2116-2118`

**Recommendation**: Add an `original_created_by` or `source_created_by` field, or store the original in the transfer transform metadata.

---

### HIGH-2: Non-Atomic Collection Transfer

**Tests**: `TestCN2_PartialTransferCorruption` (2 tests, both pass)

Collection transfer iterates over artifacts and saves each individually (lines ~1238-1244) without `transaction.atomic()`. A failure mid-way leaves:
- Some artifacts transferred, others not
- Collection potentially referencing missing artifacts
- No rollback mechanism

**Impact**: Network interruptions or process crashes during transfer leave databases in inconsistent state.

**Location**: `lamindb/models/sqlrecord.py:1238-1244`

---

### HIGH-3: Transfer Trusts Source Data Without Validation

**Tests**: `TestCE3_MaliciousInstancePoisoning` (3 tests, all pass)

Cross-instance transfers accept source records without validating:
- Hash integrity (no re-hash verification)
- UID format compliance
- Content consistency

A malicious instance could serve crafted records that pollute the target database.

**Impact**: Data poisoning via cross-instance transfer.

**Location**: `lamindb/models/sqlrecord.py:2094-2144`

---

### HIGH-4: FK Transfer Without Locking (Race Condition)

**Tests**: `TestCE2_TransferFKDanglingReference` (2 tests, both pass)

Foreign key records are transferred recursively without any locking mechanism (`select_for_update` not used). Concurrent deletion of referenced records on the source instance can create dangling FK references.

**Location**: `lamindb/models/sqlrecord.py:2128-2139`

---

### HIGH-5: Trash Resurrection via Hash Collision

**Tests**: `TestDN1_TrashResurrectionViaHashCollision` (2 tests, both pass)

Soft-deleted (trashed) records are silently resurrected when a new artifact with the same hash is created. The system:
1. Creates new artifact
2. Hits IntegrityError (duplicate hash)
3. Finds the trashed record
4. Moves it from trash (`branch_id=-1`) to default branch (`branch_id=1`)
5. Returns the OLD record (with old metadata)

The **new** description is lost — the resurrected record's metadata wins.

**Impact**: Users may unknowingly work with previously-deleted data. The new artifact's metadata (description, etc.) is silently discarded.

**Location**: `lamindb/models/sqlrecord.py:1160-1168`

---

## Medium Severity Findings

### MED-1: Artifact Lacks `is_locked` Field

**Tests**: `TestPN3_LockedRecordModification` (pass — documents absence)

`Artifact` does not have an `is_locked` attribute. Record locking only applies to certain record types at the RLS level. There is no ORM-level mechanism to lock artifacts from modification.

**Impact**: Cannot prevent artifact modification at the application level.

---

### MED-2: No Warning When Saving Data Without Space

**Tests**: `TestPI1_DefaultSpaceEscape` (2 tests, both pass)

Records created without explicit space assignment silently default to the global "all" space (accessible to every collaborator). No warning is issued.

**Impact**: Sensitive data may be inadvertently shared with all instance collaborators.

---

### MED-3: `available_spaces` Returns None on Local Instances

**Tests**: `TestPI2_RoleConflict` (pass — documents the gap)

Role-based space access is completely unavailable on local instances. `available_spaces` returns `None`, making it impossible to test or enforce role conflicts locally.

---

### MED-4: Storage `instance_uid` Is Mutable at ORM Level

**Tests**: `TestCE1_StorageInstanceUIDForgery` (2 tests, both pass)

The `instance_uid` field on Storage records (which enforces single-writer ownership) can be freely modified at the ORM level. No validation prevents a user from claiming ownership of foreign storage.

**Location**: `lamindb/models/storage.py:54-65`

---

### MED-5: Manual UID Injection Accepted

**Tests**: `TestDI1_VersionFamilyHijacking` (2 tests, both pass)

Setting `artifact.uid` to a crafted value (e.g., `"A" * 20`) before save is accepted. This could allow injection into existing version families if the crafted UID shares a stem with an existing artifact.

**Location**: `lamindb/base/uids.py`, `lamindb/models/_is_versioned.py`

---

### MED-6: Space Assignment Triggers Init Ordering Bug

**Tests**: `TestPN1_SQLiteNoAccessControl.test_space_does_not_restrict_writes_on_sqlite`

Assigning a space to an unsaved artifact can trigger `KeyError('space_id')` in `_populate_tracked_fields` (sqlrecord.py:1065), indicating `space_id` isn't in `__dict__` during initialization. This is an initialization ordering issue.

**Location**: `lamindb/models/sqlrecord.py:1065`

---

## Low Severity Findings

### LOW-1: Instance Metadata Cached in Plaintext

**Tests**: `TestCN3_StaleInstanceMetadataCache` (2 tests, both pass)

Instance connection details are cached in plaintext files. An attacker with file access could modify cache to redirect connections.

---

### LOW-2: Symlink Path Traversal Properly Blocked

**Tests**: `TestDI2_SymlinkPathTraversal` (2 tests, both pass — **no vulnerability**)

`check_path_is_child_of_root()` correctly handles:
- Symlinks pointing outside storage root (rejects via `.resolve()`)
- Relative path traversal (`../../../etc/passwd` — rejects correctly)

**Status**: Working as intended.

---

### LOW-3: SQL Injection Properly Prevented

**Tests**: `TestF3_SQLInjectionViaFields` (19 tests, all pass — **no vulnerability**)

Django's parameterized queries properly handle SQL injection payloads in:
- `description` field (stored literally)
- `filter()` operations (properly parameterized)
- `search()` operations (safely handled)
- `name` field on ULabel

**Status**: Working as intended.

---

### LOW-4: Information Disclosure in Errors

**Tests**: `TestF5_InformationDisclosure` (2 tests, both pass — **no vulnerability**)

Error messages for nonexistent records do not leak sensitive information (no connection strings, passwords, or SQL table names).

**Status**: Working as intended.

---

## Positive Findings (Security Controls Working Correctly)

| Control | Tests | Result |
|---------|-------|--------|
| SQL injection prevention | 19 parametrized tests | All parameterized correctly |
| Path traversal prevention | 10 tests (8 traversal + 2 symlink) | All blocked correctly |
| UID uniqueness | 2 tests (1000 single + 1000 concurrent) | No collisions |
| Hash deduplication under concurrency | 3 tests | Properly deduplicates |
| Suffix/dtype fuzzing resilience | 17 parametrized tests | No crashes |
| Information disclosure prevention | 2 tests | No leaks |
| Error message safety | 2 tests | No sensitive data exposed |
| Unicode handling | 8 parametrized tests | Properly handled |
| Mass record creation (100 artifacts) | 1 test | All unique, no collisions |
| Concurrent read/write safety | 2 tests | No deadlocks or corruption |
| Delete/recreate cycle consistency | 1 test | Consistently resurrects same record |

---

## Testing Gaps (Require PostgreSQL + Hub)

The following tests were skipped or could not be fully validated in the sandboxed environment:

1. **JWT token expiration race** (P-I3) — needs PG + RLS
2. **Public instance write rejection** (P-N2) — needs PG + `_db_permissions="public"`
3. **RLS bypass via direct connection** (P-E1 on PG) — needs PG RLS policies
4. **JWT token forgery** (P-E2) — needs PG security schema access
5. **Cross-instance transfer end-to-end** — needs two instances with hub connectivity
6. **Branch merge concurrency** — needs `lamindb.setup._merge` with PG transactions
7. **LAMINDB_MULTI_INSTANCE bypass** — needs multi-instance setup

---

## Recommendations Summary

| Priority | Recommendation |
|----------|---------------|
| P0 | Add DB-level triggers/constraints for critical fields (hash, uid, is_latest) to prevent `QuerySet.update()` bypass |
| P0 | Wrap collection transfers in `transaction.atomic()` |
| P1 | Preserve original `created_by` during transfers |
| P1 | Validate transferred record integrity (re-hash, UID format) |
| P1 | Add locking mechanism for FK transfers (`select_for_update`) |
| P2 | Add optional ORM-level space enforcement for SQLite |
| P2 | Warn when saving data without explicit space assignment |
| P2 | Validate UID format before accepting manual assignments |
| P2 | Make `Storage.instance_uid` read-only after initial assignment |
| P3 | Document the trash-resurrection behavior prominently |
| P3 | Fix `space_id` KeyError in init ordering |
