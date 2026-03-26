# LaminDB UX Evaluation: What Feels Unintuitive for New Users

> **Scope**: Comprehensive review of API surface, naming conventions, error handling, onboarding flow, and implicit behaviors.
>
> **Method**: Static analysis of the full codebase + documentation structure review.

---

## Executive Summary

LaminDB is a powerful system for managing biological data with versioning, lineage tracking, and access control. However, several patterns create friction for new users: **silent behavioral changes disguised as warnings**, **overlapping methods with subtle differences**, **implicit requirements that aren't enforced**, and **terminology that assumes familiarity with data registries**. This document identifies 24 issues across 6 categories, ranked by impact.

---

## 1. Naming & Terminology Confusion

### 1.1 `key` vs `name` Inconsistency (HIGH)

Different record types use different field names for the same concept ("what do I call this thing?"):

| Record Type | Human-readable identifier | Organizational path |
|-------------|--------------------------|---------------------|
| `Artifact` | `description` | `key` |
| `Transform` | `description` | `key` |
| `Collection` | `description` | `key` |
| `Run` | `name` | *(none)* |
| `ULabel` | `name` | *(none)* |
| `Feature` | `name` | *(none)* |
| `Space` | `name` | *(none)* |

**Problem**: `Run` uses `name` while `Artifact`/`Transform`/`Collection` use `key`. A new user searching for "how to name my artifact" won't find `key`. And `key` sounds like a database key, not a human-friendly label.

**Fix**: Add a `name` alias for `key` on Artifact/Transform/Collection, or unify naming across all record types. At minimum, prominently document that `key` serves as the organizational name.

### 1.2 `Artifact` Is Not Self-Explanatory (HIGH)

The word "artifact" means "something produced incidentally" in common English, or "build output" in CI/CD. In LaminDB it means "any data object" -- files, DataFrames, AnnData objects, directories.

**Fix**: The term is established and changing it would break everything, but the docs should define it on first encounter: *"An Artifact is any piece of data you want to track -- a file, a DataFrame, a trained model, a directory of images."*

### 1.3 `branch` Is Not Git-Like (MEDIUM)

LaminDB branches are organizational states (`main`, `trash`, `archive`), not isolated development environments:

```python
# Built-in branches (sqlrecord.py:1468-1560)
main     = branch_id 1   # default, visible
archive  = branch_id 0   # hidden but preserved
trash    = branch_id -1  # scheduled for deletion
```

**Problem**: Users with Git experience expect branches to provide isolated workspaces with merge operations. LaminDB branches are closer to "visibility states" or "bins".

**Fix**: Rename to `status` or `state`, or add prominent documentation: *"Branches in LaminDB are visibility states (main/archive/trash), not isolated workspaces like Git branches."*

### 1.4 `Registry` Is Jargon (LOW)

The docs use "Registry" to mean "a database table you can query." New users coming from pandas/scikit-learn don't think in database terms.

**Fix**: On first use, define: *"A Registry is a searchable catalog of records -- think of it as a smart, typed DataFrame that lives in your database."*

---

## 2. Constructor & Object Lifecycle Confusion

### 2.1 Artifact Constructor Has 13+ Parameters (HIGH)

```python
ln.Artifact(
    data,              # positional: path, DataFrame, AnnData, MuData, or dir
    key=None,          # organizational path
    description=None,  # human description
    revises=None,      # version parent
    run=None,          # provenance
    otype=None,        # object type override
    kind=None,         # "dataset" or None
    suffix=None,       # file extension override
    hash=None,         # precomputed hash
    default_storage=None,  # storage override
    using_key=None,    # DB routing key
    version_tag=None,  # explicit version label
    type=None,         # Artifact type record
)
```

**Problem**: New users don't know which parameters matter. The most common use case (save a file) requires only `data` and optionally `key`, but the signature is intimidating.

**Fix**: Highlight the 2-3 most common patterns in docs:
```python
# Most common: save a file
artifact = ln.Artifact("data.csv", key="my-project/data.csv")

# Save a DataFrame
artifact = ln.Artifact.from_dataframe(df, key="my-project/results.parquet")

# Create a new version
artifact = ln.Artifact("data_v2.csv", revises=old_artifact)
```

### 2.2 `from_dataframe()` vs Direct Constructor Produce Different Results (HIGH)

```python
# These are NOT equivalent:
a1 = ln.Artifact(df, key="data.parquet")
a2 = ln.Artifact.from_dataframe(df, key="data.parquet")
```

`from_dataframe()` sets `otype="DataFrame"`, `kind="dataset"`, calculates `n_observations`, and enables schema validation. The direct constructor does none of this.

**Problem**: No error or warning when using the direct constructor with a DataFrame. Users who skip `from_dataframe()` silently get artifacts with missing metadata.

**Fix**: Either:
1. Make `Artifact(df)` automatically delegate to `from_dataframe()` when `df` is a DataFrame, OR
2. Emit a warning: *"Consider using `Artifact.from_dataframe(df)` to get automatic metadata extraction and validation."*

### 2.3 Unsaved Objects Look Usable But Aren't (HIGH)

```python
artifact = ln.Artifact("data.csv", key="my-data")
# artifact.uid exists, artifact.path works, artifact.suffix works
# But: artifact.id is None, artifact.labels won't work, artifact.features won't work
```

The constructor succeeds and returns an object that *appears* valid. Many read operations work. But operations requiring a saved record fail with confusing errors or return empty results silently:

```python
# artifact.py:821-823 -- silently returns empty QuerySet
if self.id is None:
    return QuerySet(self.__class__)
```

**Fix**: Make unsaved objects more obviously unsaved:
1. Add a `repr` that shows `[UNSAVED]` status
2. Raise clear errors on operations that require a saved record: *"This artifact hasn't been saved yet. Call `.save()` first."*

### 2.4 Implicit Auto-Save Inconsistency (MEDIUM)

- `ln.track()` auto-saves the Transform and Run
- `ln.Artifact(...)` does NOT auto-save
- `artifact.labels.add(label)` auto-saves both the label and the link

**Problem**: Users can't predict which operations persist to the database and which don't.

**Fix**: Document a clear rule: *"Only `ln.track()` auto-saves. Everything else requires an explicit `.save()` call."* Then ensure `labels.add()` follows this rule too (or document the exception).

---

## 3. Method Naming & Overlapping APIs

### 3.1 `.load()` vs `.open()` vs `.cache()` (HIGH)

Three methods with subtle but critical differences:

| Method | Downloads? | Returns | Memory Impact |
|--------|-----------|---------|---------------|
| `.load()` | Yes | Python object (DataFrame, AnnData) | Full object in RAM |
| `.open()` | Partially | Streaming accessor (PyArrow, Polars lazy) | Low -- lazy/streaming |
| `.cache()` | Yes | `Path` to local file | None -- just a path |

**Problem**:
- `.load()` sounds like "load from disk" (which `.cache()` does)
- `.open()` sounds like "open a file" (which `.load()` does)
- `.cache()` is the clearest name but least commonly needed

**Fix**: Consider more descriptive names or add a decision guide:
```python
artifact.load()   # "I want the data in memory now"
artifact.open()   # "I want to stream/query without loading everything"
artifact.cache()  # "I want the file on disk"
```

### 3.2 `.delete()` Has Two-Stage Behavior (HIGH)

```python
artifact.delete()                    # First call: moves to trash (branch_id = -1)
artifact.delete()                    # Second call: permanently deletes from DB
artifact.delete(permanent=True)      # Skips trash, deletes immediately
artifact.delete(permanent=True, storage=True)  # Also removes from storage
```

**Problem**: Calling `.delete()` twice does different things. Most ORMs treat `.delete()` as permanent. The two-stage behavior is surprising and the parameters compound confusion.

**Fix**: Separate the methods:
```python
artifact.trash()              # Move to trash (reversible)
artifact.restore()            # Restore from trash
artifact.delete()             # Permanently delete (with confirmation/flag)
```

### 3.3 `.df()` Deprecated but Still Works (LOW)

`QuerySet.df()` is deprecated in favor of `.to_dataframe()`, but still works without warning in some code paths. Users see both in examples and don't know which is current.

**Fix**: Emit a visible deprecation warning on every `.df()` call.

### 3.4 `ln.view()` vs `QuerySet.to_dataframe()` (LOW)

- `ln.view()` displays a summary of all registries
- `ln.view(df=my_df)` displays a DataFrame with metadata annotations
- `QuerySet.to_dataframe()` converts query results to a DataFrame

These sound similar but do completely different things.

**Fix**: Rename `ln.view()` to `ln.overview()` or `ln.summary()` to distinguish it from data access methods.

---

## 4. Silent Behaviors & Missing Guardrails

### 4.1 Warnings That Should Be Errors (CRITICAL)

Several warnings represent significant behavioral changes that users can easily miss:

| Warning | What Actually Happens |
|---------|----------------------|
| *"no run & transform got linked"* | Artifact saved WITHOUT lineage tracking |
| *"didn't pass the latest version in `revises`"* | Silently replaced with latest version |
| *"renaming artifact from 'X' to 'Y'"* | Key changed without confirmation |
| *"did not add hash for {path}"* | Artifact saved without integrity hash |

**Problem**: These are logged as `WARNING` level, easily buried in output. Each represents a silent data quality compromise.

**Fix**: Convert critical behavioral changes to errors that require explicit opt-in:
```python
# Instead of silently fixing:
ln.Artifact(data, revises=old_version)  # Error: "old_version is not the latest. Pass revises=latest_version or force=True"

# Instead of silently dropping lineage:
artifact.save()  # Error: "No active run context. Call ln.track() first, or pass run=None explicitly to save without lineage."
```

### 4.2 Trash Resurrection Is Silent (HIGH)

When uploading a file whose hash matches a previously-deleted artifact, the system:
1. Creates the new artifact
2. Hits a duplicate hash constraint
3. Finds the trashed record
4. Moves it from trash to main
5. Returns the OLD record with OLD metadata

The new artifact's description/metadata is silently discarded.

**Problem**: Users expect `.save()` to persist THEIR metadata. Instead they get back a zombie record with someone else's (or their own old) metadata.

**Fix**: Warn explicitly: *"An artifact with this hash was previously deleted. Restoring it from trash. Your description 'X' was replaced with the original description 'Y'. Call `.delete(permanent=True)` first if you want to start fresh."*

### 4.3 Default Space = Global Access, No Warning (MEDIUM)

Records created without explicit `space` assignment go into the default "all" space (id=1), visible to every collaborator. No warning is issued.

**Fix**: For PostgreSQL instances with multiple spaces, warn: *"This artifact will be visible to all collaborators. Assign a space with `artifact.space = my_space` to restrict access."*

### 4.4 `QuerySet.update()` Bypasses All ORM Hooks (MEDIUM)

Django's `QuerySet.update()` skips `save()`, which means:
- No hash integrity checks
- No permission/RLS error handling
- No locked record enforcement
- No version management

**Problem**: Advanced users who discover `.update()` can accidentally corrupt data integrity.

**Fix**: Override the QuerySet manager to intercept `.update()` and validate critical fields, or document prominently: *"Always use `.save()` for individual records. `QuerySet.update()` bypasses all validation."*

### 4.5 Error Message Bug: "Space" When It Means "Branch" (LOW)

`sqlrecord.py:557` says "Space" when the error is about a Branch:

```python
# Line 557: error message references "Space" but the issue is about branches
```

**Fix**: Correct the error message.

---

## 5. Onboarding & Documentation Gaps

### 5.1 No 5-Minute Quickstart (HIGH)

The current getting-started flow requires:
1. Install lamindb
2. `lamin init` (CLI setup)
3. Understand instances, storage, transforms
4. Call `ln.track()`
5. Create artifacts

**Problem**: A pandas user just wants to track their data. They don't care about transforms or instances yet.

**Fix**: Add a zero-config quickstart:
```python
import lamindb as ln
ln.setup("my-project")  # One-line local setup

# Track a DataFrame
df = pd.read_csv("experiment.csv")
artifact = ln.Artifact.from_dataframe(df, key="experiments/run1.parquet")
artifact.save()

# Find it later
ln.Artifact.filter(key__contains="experiment").to_dataframe()
```

### 5.2 Core Concepts Undefined at Point of First Encounter (HIGH)

The docs use specialized terms without definition:
- **Artifact**: First used without explaining it's "any tracked data object"
- **Registry**: Used without explaining it's "a queryable table of records"
- **Transform**: Used without explaining it's "a script/notebook/pipeline step"
- **Run**: Used without explaining it's "one execution of a transform"

**Fix**: Add inline definitions on first use, plus a glossary page linked from the quickstart.

### 5.3 `ln.track()` Purpose Not Obvious (HIGH)

New users see `ln.track()` at the top of every example but don't understand why:
- What happens if I skip it? (Artifacts save but without lineage -- just a warning)
- When do I need it? (Always, if you want provenance tracking)
- What does it actually do? (Creates/loads a Transform + Run, sets global context)

**Fix**: Explain upfront: *"`ln.track()` tells LaminDB which script/notebook is running, so it can automatically track data lineage. Skip it if you just want to store data without provenance."*

### 5.4 Django Knowledge Assumed (MEDIUM)

Error messages and query syntax assume Django ORM familiarity:
- `ObjectDoesNotExist` exception (Django-specific)
- `filter(key__contains="x")` double-underscore syntax
- `ProgrammingError` from database layer leaking through

**Fix**: Wrap Django exceptions in LaminDB-specific errors. Document the query syntax as a "query language" rather than assuming users know Django:
```python
# Instead of:
raise ObjectDoesNotExist("Artifact matching query does not exist")
# Raise:
raise ln.errors.RecordNotFound("No artifact found matching your query")
```

### 5.5 FAQ Reveals Unaddressed Pain Points (LOW)

The FAQ includes questions that should be addressed in the main docs:
- "What are the differences to AWS S3?"
- "What are the differences to DVC?"
- "Can I sync my git repository with a lamindb instance?"

These indicate conceptual gaps that new users regularly hit.

**Fix**: Add a "LaminDB vs X" comparison page and a "mental model" guide that positions LaminDB relative to tools users already know.

---

## 6. Version Management Confusion

### 6.1 Two Ways to Create Versions, Different Behavior (HIGH)

```python
# Way 1: Pass `key` -- auto-versions if content differs
a1 = ln.Artifact("v1.csv", key="data.csv").save()
a2 = ln.Artifact("v2.csv", key="data.csv").save()  # Auto-creates version 2

# Way 2: Pass `revises` -- explicit versioning
a2 = ln.Artifact("v2.csv", revises=a1).save()
```

**Problem**:
- Way 1 is implicit -- users may not realize they're creating versions
- Way 2 requires keeping a reference to the previous artifact
- Passing both `key` AND `revises` with a different key silently renames (just a warning)
- If content is identical, Way 1 returns the EXISTING artifact (no new version)

**Fix**: Make the two paths clearly documented with decision guidance: *"Use `key=` for automatic versioning by path. Use `revises=` for explicit version chains. Don't mix them."*

### 6.2 Version Tags Are Random Strings by Default (MEDIUM)

```python
@property
def version(self) -> str:
    return self.version_tag if self.version_tag else self.uid[-4:]
```

If you don't set `version_tag`, the "version" is the last 4 characters of the UID -- a random base62 string like `"a7Kx"`.

**Problem**: Users expect version numbers like `1`, `2`, `3` or `1.0`, `1.1`. Seeing `a7Kx` as a version is confusing.

**Fix**: Auto-increment a human-readable version number (1, 2, 3...) as default, or prompt users to set one.

### 6.3 `is_latest` Flag Can Get Out of Sync (LOW)

The `is_latest` flag on versioned records is managed in `save()` logic. If bypassed (via `QuerySet.update()` or raw SQL), multiple records in a family can have `is_latest=True`.

**Fix**: Add a database constraint (if possible) or a periodic consistency check.

---

## Priority Matrix

| # | Issue | Severity | Effort | Impact |
|---|-------|----------|--------|--------|
| 4.1 | Warnings that should be errors | CRITICAL | Medium | Prevents silent data quality issues |
| 2.2 | `from_dataframe()` vs constructor divergence | HIGH | Low | Prevents missing metadata |
| 2.3 | Unsaved objects look usable | HIGH | Medium | Prevents confusing silent failures |
| 5.1 | No 5-minute quickstart | HIGH | Medium | Reduces onboarding friction |
| 5.2 | Core concepts undefined | HIGH | Low | Reduces documentation friction |
| 3.2 | Two-stage `.delete()` | HIGH | Medium | Matches user expectations |
| 4.2 | Silent trash resurrection | HIGH | Low | Prevents data loss |
| 6.1 | Two versioning paths | HIGH | Low | Documentation only |
| 1.1 | `key` vs `name` inconsistency | HIGH | High | Long-term API clarity |
| 5.3 | `ln.track()` purpose unclear | HIGH | Low | Documentation only |
| 3.1 | `.load()` / `.open()` / `.cache()` | HIGH | Low | Documentation + possible rename |
| 2.1 | 13-parameter constructor | HIGH | Low | Documentation only |
| 2.4 | Auto-save inconsistency | MEDIUM | Medium | Predictable behavior |
| 1.3 | `branch` misnomer | MEDIUM | High | Would require migration |
| 4.3 | Default space = global, no warning | MEDIUM | Low | Quick win |
| 4.4 | `QuerySet.update()` bypass | MEDIUM | Medium | Requires custom manager |
| 6.2 | Random version tags | MEDIUM | Medium | Better defaults |
| 5.4 | Django knowledge assumed | MEDIUM | Medium | Better error wrapping |
| 1.2 | "Artifact" not self-explanatory | HIGH | Low | Documentation only |
| 1.4 | "Registry" jargon | LOW | Low | Documentation only |
| 3.3 | `.df()` still works | LOW | Low | Add deprecation warning |
| 3.4 | `ln.view()` ambiguity | LOW | Low | Rename consideration |
| 4.5 | Error message bug | LOW | Low | Quick fix |
| 6.3 | `is_latest` sync | LOW | Medium | DB constraint |

---

## Top 5 Quick Wins (High Impact, Low Effort)

1. **Convert critical warnings to errors** (4.1) -- Prevent silent lineage loss, silent renaming, and silent version substitution
2. **Auto-delegate `Artifact(df)` to `from_dataframe()`** (2.2) -- One `isinstance` check prevents missing metadata
3. **Add inline definitions for Artifact, Registry, Transform, Run** (5.2) -- Pure documentation change
4. **Add trash resurrection warning** (4.2) -- One `logger.warning()` call with actionable message
5. **Document `ln.track()` purpose prominently** (5.3) -- Add a one-paragraph explanation to quickstart

---

## Top 3 Structural Improvements (High Impact, Higher Effort)

1. **Separate `.trash()` / `.restore()` / `.delete()`** (3.2) -- Matches every other ORM's mental model
2. **Make unsaved objects clearly distinguishable** (2.3) -- `repr` changes + early errors on invalid operations
3. **Add zero-config quickstart** (5.1) -- `ln.setup("project")` one-liner that creates a local instance with sensible defaults
