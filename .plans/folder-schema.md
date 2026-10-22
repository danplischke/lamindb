# Folder artifact schemas implementation plan

**Goal:** Validate required child objects against individual Lamin schemas, then register their directory as one schema-validated artifact.

**Architecture:** Represent a folder as `Schema(otype="Folder", slots={relative_child_path: child_schema})`. Implement a `FolderCurator` that delegates to existing object-type curators and reuses their component-validation and annotation machinery, with a dedicated save path for directory artifacts. Add `Artifact.from_folder()` without changing the existing meaning of `Artifact.from_dir()`.

**Tech stack:** Python >=3.10, Django models, existing Lamin schemas and curators, pandas, optional existing Polars integration, pytest, Ruff.

## Approved scope

The initial scope was table-only. The user subsequently approved generalized
delegation by child `otype`. The implementation supports DataFrame, AnnData,
MuData, SpatialData, TileDB-SOMA and dictionary children through existing
loaders/curators and their optional dependencies. Directory-backed children
are single manifest entries, and inferred slots are path-qualified (for example,
`expression.zarr::obs`). Unsupported custom object types fail explicitly.

The detailed task examples below retain the initial table-first progression;
their table-only loading restrictions are superseded by this amendment.

Included:

- All declared file paths are required.
- Each path maps to an existing saved table schema.
- Exact relative file paths, including nested directories.
- A local directory or an existing artifact whose directory is local.
- Storage formats supported by existing loaders, with optional suffix constraints.
- Per-file column, datatype, index and categorical checks through existing curation.
- Optional rejection of undeclared files with the folder schema's `maximal_set=True`.
- One directory artifact carrying the validating schema and path-scoped inferred schemas.
- Read-only validation: do not silently standardize or rewrite source files.
- Explicit errors with the failing relative path.

Excluded from this PR:

- Cross-file foreign keys, sample alignment and uniqueness relationships.
- Optional files, globs, partitioned Parquet datasets and nested folder schemas.
- Direct remote-folder validation or implicit downloads. A user may explicitly cache an artifact first.
- New manifest registries, migrations, validation-report models or persisted validation timestamps.
- Automatic validation every time an arbitrary `Artifact(path, schema=...)` is saved.

This is validation at the explicit folder-curation entry points, not a database-wide enforcement mechanism. Existing generic artifact constructors retain their behavior.

## Existing implementation and constraints

1. `lamindb/models/schema.py`
   - `Schema.slots` already persists named child schemas through `SchemaComponent`.
   - Slot names already participate in schema hashes.
   - `otype` deliberately does not participate in existing hashes.
   - `Schema` is mutable, not a versioned registry. A changed contract must be created as a separate schema rather than editing a schema referenced by old artifacts.
2. `lamindb/models/artifact.py`
   - `Artifact.from_dir()` returns a `SQLRecordList`: one artifact per file.
   - `Artifact(path)` already handles directory hashing, storage and versioning.
   - `_can_scan_lazily()` and `_scan_dataframe_file()` provide existing optional Polars paths.
   - `Artifact.from_dataframe()` can validate paths, but calling its save path for each child would introduce unwanted child-artifact registration.
3. `lamindb/curators/core.py`
   - `Curator.__init__()` currently rejects generic folder artifacts.
   - `SlotsCurator` validates and annotates `ComponentCurator` instances.
   - `SlotsCurator.save_artifact()` only knows table/scverse constructors; it cannot construct a folder artifact.
   - `annotate_artifact()` already writes inferred schemas keyed by a slot.
4. `lamindb/curators/__init__.py`
   - Public curators are lazy exports; preserve this import behavior.
5. `tests/pydata/conftest.py`
   - Initializes a dedicated SQLite test instance by default.
   - Tests modify test-instance settings and remove their test storage during teardown.
   - Run from the LaminDB checkout, not the previous iFOXDB working directory.

## Proposed public API

```python
import lamindb as ln

sample_schema = ln.Schema(
    features=[
        ln.Feature(name="sample_id", dtype=str).save(),
        ln.Feature(name="cohort", dtype=str).save(),
    ],
    otype="DataFrame",
    suffix=".parquet",
).save()

expression_schema = ln.Schema(
    features=[
        ln.Feature(name="sample_id", dtype=str).save(),
        ln.Feature(name="protein_id", dtype=str).save(),
        ln.Feature(name="value", dtype=float).save(),
    ],
    otype="DataFrame",
    suffix=".parquet",
).save()

folder_schema = ln.Schema(
    name="Proteomics tables v1",
    otype="Folder",
    slots={
        "samples.parquet": sample_schema,
        "expression.parquet": expression_schema,
    },
    maximal_set=True,
).save()

# Constructor convention: returns an unsaved artifact.
artifact = ln.Artifact.from_folder(
    "./proteomics",
    key="studies/proteomics",
    schema=folder_schema,
).save()

# Curator convention: save_artifact() persists the artifact.
curator = ln.curators.FolderCurator("./proteomics", folder_schema)
curator.validate()
artifact = curator.save_artifact(key="studies/proteomics")

# Revalidation of a local directory artifact:
ln.curators.FolderCurator(artifact, folder_schema).validate()
```

The table examples deliberately use long-form expression data. The feature does not impose that shape: wide protein matrices are supported whenever their child schema can express the desired checks.

### Behavioral contract

- `FolderCurator.validate()` returns `None` on success, matching existing composite curators.
- Invalid schema or unsupported input configuration raises `InvalidArgument`.
- Missing files and invalid table content raise `ValidationError` with the relative path.
- Preserve actual filesystem/read errors as their original exception types, adding path context only when it does not obscure the cause.
- Folder `minimal_set` must be `True`; optional-file semantics are deferred.
- Folder `ordered_set=True` is rejected: directories do not define file ordering.
- Folder `maximal_set=False` permits undeclared files; they are stored but not content-validated.
- Folder `maximal_set=True` requires the actual set of files to equal the declared set.
- Empty directories do not count as additional files.
- Child `otype` may be `None` or `"DataFrame"`, consistent with simple tabular schemas.
- Child schema suffix, when specified, must match the file suffix.
- Child schemas with nested slots or enabled value coercion/default insertion are rejected in v1. Accepting transformed in-memory values would otherwise certify bytes that were never changed on disk.
- Reject folder-level features, index, datatype/coercion settings that would imply table semantics. Folder validation uses slots, not a second table schema layered over the directory.
- Reject reserved `__external__` as a filename in this first version; external feature schema support is deferred, not silently interpreted as a file.
- Reject empty or absolute slot paths, `.`/`..` segments, repeated separators, backslashes, URI paths, wildcard characters and symlinks. Never normalize an unsafe path into a valid one.
- Reject remote paths explicitly. Existing remote directory artifacts must be explicitly cached; resolve their already-cached local directory if available without initiating a download.
- Validation must not register child artifacts.
- A failed content check must not change the folder artifact's schema or annotations.
- Every save entry point revalidates current on-disk content, even after a prior successful `validate()`. This catches ordinary edits between validation and saving; it does not promise transactional snapshots against concurrent external writers.
- Do not promise atomic rollback of object-store writes via Django transactions.

## File map

| File | Responsibility |
| --- | --- |
| `lamindb/models/schema.py` | Validate folder schema configuration and add folder-only hash discrimination |
| `lamindb/curators/_folder.py` (new) | Resolve local roots, validate layout, read tables, orchestrate component curation, prepare folder artifacts |
| `lamindb/curators/core.py` | Narrow folder-artifact resolution hook and reuse/extract annotation preparation if necessary |
| `lamindb/curators/__init__.py` | Lazy public export and API documentation for `FolderCurator` |
| `lamindb/models/artifact.py` | Unsaved `Artifact.from_folder()` constructor and artifact documentation |
| `tests/pydata/test_folder_curation.py` (new) | Layout, content, persistence and regression tests |
| `tests/pydata/test_schema.py` | Folder schema identity, configuration and compatibility tests |
| `docs/scripts/curate_folder.py` (new) | Executable public example |
| `docs/curate.md` | Explain folder curation and explicitly deferred guarantees |
| `docs/organize.md` | Distinguish one folder artifact from `from_dir()` and collections |

No database migration is expected: existing string `otype`, `SchemaComponent` and `ArtifactSchema` fields can represent the design.

## Task 1: Lock down folder schema identity and declaration semantics

**Files:** `lamindb/models/schema.py`, `tests/pydata/test_schema.py`.

1. Add failing tests for a saved folder schema round-trip:

   ```python
   def test_folder_schema_roundtrip():
       feature = ln.Feature(name="folder_test_sample_id", dtype=str).save()
       child = ln.Schema(features=[feature], otype="DataFrame").save()
       schema = ln.Schema(
           otype="Folder", slots={"samples.csv": child}, maximal_set=True
       ).save()
       loaded = ln.Schema.get(schema.uid)
       assert loaded.otype == "Folder"
       assert loaded.slots["samples.csv"] == child
       assert loaded.maximal_set is True
   ```

2. Add identity tests:
   - Same slots in different dictionary insertion orders yield the same hash.
   - Renaming a path changes the hash.
   - Changing a child schema changes the parent hash.
   - Toggling `maximal_set` changes the hash.
   - A folder schema cannot collide with a non-folder composite schema with otherwise identical slots.
   - Existing non-folder schema hash expectations remain unchanged.

   The collision regression should construct the non-folder schema first:

   ```python
   def test_folder_schema_hash_is_distinct():
       feature = ln.Feature(name="folder_hash_sample_id", dtype=str).save()
       child = ln.Schema(features=[feature]).save()
       other = ln.Schema(
           otype="OtherComposite", slots={"samples.csv": child}
       ).save()
       folder = ln.Schema(
           otype="Folder", slots={"samples.csv": child}
       ).save()
       assert folder.hash != other.hash
       assert folder.otype == "Folder"
   ```

3. Run RED:

   ```bash
   cd /Users/PLISCD/Desktop/lamindb
   .venv/bin/python -m pytest tests/pydata/test_schema.py -k folder -q
   ```

   Expected: the folder/non-folder hash distinction fails before implementation.

4. Validate folder configuration before hash lookup. Add a folder-only hash token such as `otype=Folder`; do not include `otype` in hashes for every existing schema. Validate unsafe slot paths and unsupported settings both at schema construction and when the curator consumes a reloaded/mutated schema.

5. Parameterize the rejected paths:

   ```python
   @pytest.mark.parametrize(
       "slot",
       [
           "", "/samples.csv", "../samples.csv", "a/../samples.csv",
           "./samples.csv", "a//samples.csv", r"a\samples.csv",
           "s3://bucket/samples.csv", "*.csv", "__external__",
       ],
   )
   def test_folder_schema_rejects_unsafe_slots(slot):
       feature = ln.Feature(name="folder_path_sample_id", dtype=str).save()
       child = ln.Schema(features=[feature]).save()
       with pytest.raises(ln.errors.InvalidArgument):
           ln.Schema(otype="Folder", slots={slot: child})
   ```

6. Run GREEN and the full schema regression file:

   ```bash
   .venv/bin/python -m pytest tests/pydata/test_schema.py -q
   ```

   Expected: all schema tests pass, including existing literal hash assertions.

**Suggested commit:** `feat: define folder schemas using path slots`

## Task 2: Validate folder layout and child table content

**Files:** new `lamindb/curators/_folder.py`, `lamindb/curators/__init__.py`, new `tests/pydata/test_folder_curation.py`.

1. Create reusable test fixtures with tiny local files:

   ```python
   import lamindb as ln
   import pandas as pd
   import pytest

   @pytest.fixture
   def folder_input(tmp_path):
       root = tmp_path / "study"
       root.mkdir()
       pd.DataFrame({"sample_id": ["s1", "s2"]}).to_parquet(
           root / "samples.parquet", index=False
       )
       pd.DataFrame({
           "sample_id": ["s1", "s2"],
           "protein_id": ["P16860", "P19429"],
           "value": [1.0, 2.0],
       }).to_parquet(root / "expression.parquet", index=False)
       sample_id = ln.Feature(name="sample_id", dtype=str).save()
       protein_id = ln.Feature(name="protein_id", dtype=str).save()
       value = ln.Feature(name="value", dtype=float).save()
       samples = ln.Schema(
           features=[sample_id], otype="DataFrame", suffix=".parquet"
       ).save()
       expression = ln.Schema(
           features=[sample_id, protein_id, value],
           otype="DataFrame", suffix=".parquet",
       ).save()
       schema = ln.Schema(
           otype="Folder",
           slots={"samples.parquet": samples, "expression.parquet": expression},
           maximal_set=True,
       ).save()
       return root, schema

   def test_folder_validate_does_not_register_artifacts(folder_input):
       root, schema = folder_input
       before = ln.Artifact.objects.count()
       assert ln.curators.FolderCurator(root, schema).validate() is None
       assert ln.Artifact.objects.count() == before

   def test_folder_missing_required_file(folder_input):
       root, schema = folder_input
       (root / "samples.parquet").unlink()
       with pytest.raises(ln.errors.ValidationError, match="samples.parquet"):
           ln.curators.FolderCurator(root, schema).validate()
   ```

2. Run RED:

   ```bash
   .venv/bin/python -m pytest tests/pydata/test_folder_curation.py -q
   ```

   Expected: missing `FolderCurator` export/implementation fails.

3. Implement `FolderCurator` in `_folder.py` as a `SlotsCurator` subclass:
   - Initialize the base with the resolved local root, not an unsupported folder artifact.
   - Retain an existing artifact separately when one was passed.
   - Resolve and validate the file layout before opening children.
   - Read Parquet using existing pandas/Polars capabilities; read CSV/TSV with the explicit appropriate delimiter.
   - Prefer existing lazy table helpers when compatible, without making Polars a new required dependency. Do not call child artifact-save methods.
   - Reuse `ComponentCurator` for each table, keyed by the complete relative path.
   - For validation failures, add that path and retain the original exception as the cause.
   - Reset `_is_validated=False` and reconstruct children on every validation attempt.
   - Process children one at a time where possible; document any retained table memory. Do not promise streaming for flexible schemas requiring pandas.
   - Reuse existing generic annotation code; do not add a parallel categorical validation engine.

4. Add the lazy export in `__all__`, `TYPE_CHECKING` imports and `__getattr__`. Load `_folder` only for `FolderCurator`; all other names continue to load from `core`.

5. Extend tests for:
   - CSV, TSV and Parquet equivalent data.
   - Nested exact paths.
   - Missing columns, invalid datatypes, unregistered categorical values, child suffix mismatches.
   - Declared file replaced with a directory.
   - Strict versus permissive undeclared files.
   - Wrong schema `otype`, unsaved schemas and unsupported child schemas.
   - Rejected value-coercion/default settings.
   - Symlinked root, child, intermediate directory and undeclared symlink.
   - Remote input rejected before network I/O.
   - A failed revalidation resets state even after earlier success.

6. Run GREEN:

   ```bash
   .venv/bin/python -m pytest tests/pydata/test_folder_curation.py -q
   ```

   Expected: layout/content tests pass without child-artifact creation.

**Suggested commit:** `feat: validate table folders with FolderCurator`

## Task 3: Register and annotate one folder artifact

**Files:** `lamindb/curators/_folder.py`, `lamindb/curators/core.py` only for directly necessary shared hooks, `lamindb/models/artifact.py`, `tests/pydata/test_folder_curation.py`.

1. Add a failing constructor/save regression:

   ```python
   def test_from_folder_returns_unsaved_single_artifact(folder_input):
       root, schema = folder_input
       before = ln.Artifact.objects.count()
       artifact = ln.Artifact.from_folder(
           root, schema=schema, key="folder-tests/constructor"
       )
       assert artifact._state.adding
       assert ln.Artifact.objects.count() == before
       artifact.save()
       assert ln.Artifact.objects.count() == before + 1
       assert artifact.schema == schema
       assert artifact.otype == "Folder"
       assert artifact.n_files == 2
   ```

2. Add a mutation regression for both `curator.save_artifact()` and the unsaved constructor's later `.save()`:

   ```python
   def test_folder_save_revalidates_disk(folder_input):
       root, schema = folder_input
       curator = ln.curators.FolderCurator(root, schema)
       curator.validate()
       (root / "samples.parquet").unlink()
       with pytest.raises(ln.errors.ValidationError, match="samples.parquet"):
           curator.save_artifact(key="folder-tests/changed")
       assert not ln.Artifact.filter(key="folder-tests/changed").exists()
   ```

   Repeat using `artifact = Artifact.from_folder(...)`, alter the file, then call `artifact.save()`. Assert no validated artifact or inferred-schema links are persisted.

3. Run RED:

   ```bash
   .venv/bin/python -m pytest tests/pydata/test_folder_curation.py -q
   ```

   Expected: constructor and folder save-path tests fail.

4. Add `Artifact.from_folder(path, *, schema, key=None, description=None, run=None, revises=None)`:
   - Require a folder schema.
   - Validate before artifact registration.
   - Use existing directory artifact construction, hashing, deduplication and revision handling.
   - Set `otype="Folder"`.
   - Stage the curator/schema for the later `.save()` using the existing staged metadata pattern.
   - Add a narrowly scoped save-time hook for these staged folder curators to revalidate before SQL or storage mutation, then apply annotations after the artifact is saved.
   - Keep the constructor's return value unsaved. Do not implement it by calling `save_artifact()`.

5. Implement `FolderCurator.save_artifact()` via the same preparation/save lifecycle:
   - Revalidate the directory.
   - Prepare the folder artifact, or reuse the existing folder artifact being curated.
   - Apply `artifact.schema` only after validation succeeds.
   - Preserve the existing save semantics for keys, revisions, runs and duplicate hashes.
   - Persist inferred schemas under `samples.parquet` and `expression.parquet`, not under an ambiguous shared `columns` slot.
   - Collect label vectors with path-qualified internal keys so identical column names in different files do not overwrite one another.
   - Reuse `annotate_artifact()` for links; extract a small helper only if needed to share annotation preparation with the existing save path.
   - Clear staged state after successful save and verify repeat saves remain safe.

6. Add tests for:
   - `save_artifact()` yields exactly one new artifact.
   - Reloaded `artifact.schema` and path-scoped inferred schemas.
   - Identically named categorical columns across two files retain all appropriate labels.
   - Revalidation of an existing generic local folder artifact.
   - Existing cached remote folder artifact: local access only, no auto-download.
   - Existing artifact failure leaves its original schema and links unchanged.
   - Deduplication and `revises` follow ordinary directory-artifact behavior.
   - Changes to directory content create the normal new artifact version.
   - `Artifact.from_dir()` still returns per-file artifacts.
   - A byte digest of the source tree is unchanged by validation/saving.

7. Run GREEN and directly coupled regressions:

   ```bash
   .venv/bin/python -m pytest \
     tests/pydata/test_folder_curation.py \
     tests/pydata/test_schema.py \
     tests/pydata/test_artifact_basics.py \
     tests/pydata/test_artifact_dataframe_with_curation.py \
     tests/pydata/test_curator_basics.py -q
   ```

   Expected: all selected tests pass; no changed semantics for existing curators or `from_dir()`.

**Suggested commit:** `feat: register schema-validated folder artifacts`

## Task 4: Document the contract and test the example

**Files:** `docs/scripts/curate_folder.py`, `docs/curate.md`, `docs/organize.md`, `lamindb/curators/__init__.py`, `lamindb/models/artifact.py`, `tests/pydata/test_folder_curation.py`.

1. Create an executable documentation script based on the public example above, using two-row local tables in a `TemporaryDirectory`. Save/retrieve the folder artifact and assert its validating schema and `n_files`.
2. Execute the script from a pytest test using the repository's existing documentation-script execution pattern. Verify the test fails before the script exists, then passes after adding it.
3. Add a folder curation section using `literalinclude` for the script.
4. Explicitly document:
   - A directory is the storage unit; `slots` select tables inside it.
   - Which files/formats are required, how undeclared files behave and which failures surface.
   - Content validation is read-only and does not repair source files.
   - Local-only scope and explicit caching for remote inputs.
   - A folder schema does not check sample alignment across files.
   - Generic `Artifact(path, schema=...)` does not imply content validation.
   - Use a newly created schema record for a changed contract; do not claim immutable schema versions.
   - Use `from_dir()` or a collection when independent child artifact identity is desired.
5. Add `FolderCurator` to the curator API documentation and link `Artifact.from_folder()` from the artifact constructor docs.
6. Verify:

   ```bash
   .venv/bin/python -m pytest tests/pydata/test_folder_curation.py -q
   .venv/bin/python -m ruff check \
     lamindb/curators/_folder.py lamindb/curators/__init__.py \
     lamindb/curators/core.py lamindb/models/schema.py \
     lamindb/models/artifact.py tests/pydata/test_folder_curation.py \
     tests/pydata/test_schema.py docs/scripts/curate_folder.py
   .venv/bin/python -m ruff format --check \
     lamindb/curators/_folder.py lamindb/curators/__init__.py \
     lamindb/curators/core.py lamindb/models/schema.py \
     lamindb/models/artifact.py tests/pydata/test_folder_curation.py \
     tests/pydata/test_schema.py docs/scripts/curate_folder.py
   ```

   Expected: tests pass, no lint findings, all selected files formatted.

**Suggested commit:** `docs: explain schema-validated table folders`

## Final acceptance and review

- Run the combined targeted regression command from Task 3 again after documentation/example wiring.
- If shared curator or artifact-save behavior changed, escalate to the full `tests/pydata` suite.
- Use the existing PostgreSQL CI group for backend coverage; SQLite alone does not establish PostgreSQL correctness.
- Verify no migrations are generated, no dependencies were added and no existing schema hashes changed.
- Inspect the diff for accidental remote downloads, broad exception fallbacks and source-file writes.
- Confirm imported `lamindb` still avoids loading pandas/curator internals until needed.
- Confirm every claimed check exists as an executable test, especially constructor/save persistence and read-only behavior.
- Do not run tests against the user's live Lamin instance. The documented pytest fixture owns an isolated test instance, but its initialization changes active settings; run with that side effect understood.

No tests were run while drafting this plan. All commands above are proposed implementation verification steps, not reported passing results.

## Follow-up boundary

Plan cross-file relationships as a separate feature after this API is exercised. It should address reference columns, index references, duplicate/null key policies, subset versus exact-match semantics, scalable execution and queryable constraint persistence. In particular, the real Olink data has 21 expression participants without disease-status rows, so an unconditional equality requirement would be incorrect.

Optional files and path patterns also require explicit persisted semantics; do not introduce them through un-hashed auxiliary metadata or undocumented conventions in this PR. AnnData children are implemented as part of the approved scope amendment above.
