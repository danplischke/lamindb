# Folder-Based Storage Pitfalls in LaminDB

> For users accustomed to local filesystems, S3 buckets, NAS drives, or any path-based file organization.

---

## The Core Mental Model Shift

In a traditional filesystem, **the path IS the identity**. You find files by navigating folders. In LaminDB, **the hash IS the identity** and the path is optional metadata. This single difference cascades into every pitfall below.

```
Traditional:  /projects/experiment-1/results/model.pkl  ← this IS the file
LaminDB:      .lamindb/tCUkRcaEjTjhtozp0000.pkl        ← this is the file
              key="projects/experiment-1/results/model.pkl"  ← this is a label
```

---

## Pitfall 1: "I Uploaded Two Files to Different Folders but Only Got One Artifact"

**What happens:**
```python
a1 = ln.Artifact("model.pkl", key="team-a/model.pkl").save()
a2 = ln.Artifact("model.pkl", key="team-b/model.pkl").save()   # same file!
# a2 IS a1 — same hash means same artifact, second key is silently ignored
```

**Why**: LaminDB deduplicates by content hash. If two files are byte-identical, they resolve to the same artifact regardless of key. The second `key` is quietly discarded.

**What filesystem users expect**: Two independent files at two independent paths.

**The trap deepens**: If you later delete `a1`, you've also deleted `a2` — they're the same record. Users who organized data into "team-a/" and "team-b/" folders lose their organizational structure.

**Workaround**: Pass `skip_hash_lookup=True` to force separate artifacts for identical content. But now you have duplicated storage.

---

## Pitfall 2: "I Can't Move or Rename My Files"

**What happens:**
```python
artifact = ln.Artifact("data.csv", key="old/location/data.csv").save()
artifact.key = "new/location/data.csv"
artifact.save()
# WARNING: "key old/location/data.csv on existing artifact differs..."
# Key is NOT changed. The artifact stays at "old/location/data.csv".
```

In a filesystem, `mv old/data.csv new/data.csv` is trivial. In LaminDB, keys are quasi-immutable after creation. The only way to "move" a file is to create a new version with a different key — which triggers a warning about renaming and creates a version chain you didn't want.

**Why this matters**: Teams that reorganize project structures (renaming `experiment-1/` to `2024-q1-results/`) can't bulk-rename without creating phantom version chains.

**What you actually have to do**:
```python
# There's no clean path. You'd need to:
new = ln.Artifact("data.csv", key="new/location/data.csv", revises=artifact).save()
# But now artifact is version 1, new is version 2 — they're "versions" of each other
# when really you just wanted to rename the folder
```

---

## Pitfall 3: "Where Are My Files? I Can't Browse the Storage"

**What happens when you look at your storage:**
```
my-storage/
├── .lamindb/
│   ├── tCUkRcaEjTjhtozp0000.csv
│   ├── a7KxBnWqPr9mYzDe0001.h5ad
│   ├── Qm3nF8vRtH2pJxKc0000.pkl
│   └── ...hundreds of opaque UID files...
```

There are no folders. No human-readable names. The key hierarchy (`experiments/2024/trial-1/results.csv`) exists only in the database, not on disk.

**What filesystem users expect**: A storage directory that mirrors their key hierarchy:
```
my-storage/
├── experiments/
│   └── 2024/
│       └── trial-1/
│           └── results.csv
```

**There's no `ls` equivalent**:
```python
# This doesn't exist:
ln.Artifact.ls("experiments/2024/")

# You have to do:
ln.Artifact.filter(key__startswith="experiments/2024/").to_dataframe()
```

**No tree view**: There's no `tree` command to visualize the hierarchy. You must construct it yourself from query results.

**No `cd`**: There's no concept of "current directory" within LaminDB. Every query starts from the root.

---

## Pitfall 4: "I Deleted a File but It's Still in Storage" / "I Deleted a File and It Took Other Versions With It"

Delete behavior depends on three factors: `_key_is_virtual`, `_overwrite_versions`, and `is_latest`:

| Scenario | What `.delete()` Does |
|---|---|
| First `.delete()` call | Moves to trash (`branch_id=-1`). File stays in storage. |
| Second `.delete()` call | Permanent DB delete. May or may not delete storage. |
| Virtual key artifact | Storage deleted automatically |
| Non-virtual key artifact | **Prompts for confirmation** via `input()` (!!) |
| Folder with `overwrite_versions=True`, latest version | Deletes ALL versions' DB records + storage |
| Folder with `overwrite_versions=True`, old version | Deletes ONLY this version's DB record. Storage untouched (shared). |

**The worst case**: You have a folder artifact with 5 versions sharing the same storage directory. You delete version 3 expecting just that version to go away. The DB record is removed but storage is untouched (because versions share it). Now version 3's key points to data that still exists but has no record. If you then delete the latest version, ALL storage is wiped — including data that versions 1, 2, and 4 might have referenced.

**What filesystem users expect**: `rm file.csv` removes the file. Period.

---

## Pitfall 5: "I Uploaded a Directory but Can't Treat It as a Directory"

**`from_dir()` creates individual artifacts, not a folder object:**
```python
# Upload a directory
artifacts = ln.Artifact.from_dir("./experiment-results/", key="results")

# This creates one artifact PER FILE:
# key="results/train.csv", key="results/test.csv", key="results/model.pkl"

# You CANNOT do:
folder = ln.Artifact.get(key="results")         # No such artifact
folder.path.iterdir()                            # Doesn't work
```

**There IS a separate folder artifact concept** (n_objects > 1, suffix=None), but it's created differently:
```python
# This creates ONE artifact representing the whole directory
folder_artifact = ln.Artifact("./experiment-results/", key="results").save()
# folder_artifact.n_objects = 3  (number of files)
```

**The confusion**: `from_dir()` and `Artifact(dir_path)` do completely different things with the same input (a directory path). One creates N artifacts, the other creates 1 artifact.

---

## Pitfall 6: "I Changed a File In-Place and Nothing Updated"

**What happens:**
```python
artifact = ln.Artifact("data.csv", key="data.csv").save()

# User edits data.csv locally...
# Then expects LaminDB to notice:
artifact.load()  # Returns CACHED version, not the edited file

# Or worse, creates "new" artifact from same path:
artifact2 = ln.Artifact("data.csv", key="data.csv").save()
# If file changed: creates a new VERSION (not what user wanted)
# If file unchanged: returns original artifact (deduplication)
```

**Why**: LaminDB copies files into managed storage (`.lamindb/`) on save. The original file is independent after that. There's no "sync" or "watch" mechanism.

**What filesystem users expect**: The file they saved is the file they get. Edit the file, get the edited version.

**The `.replace()` escape hatch exists but is hidden**:
```python
artifact.replace("data_edited.csv")  # Replaces content WITHOUT creating a version
artifact.save()
```

Most users won't discover `.replace()` because it's not the obvious thing to try.

---

## Pitfall 7: "Same Key = New Version, Even When I Didn't Want Versioning"

```python
# First upload
ln.Artifact("results_v1.csv", key="results.csv").save()

# Weeks later, different experiment, same filename habit:
ln.Artifact("results_v2.csv", key="results.csv").save()
# Surprise: this is now version 2 of the FIRST artifact

# They share a version chain:
ln.Artifact.get(key="results.csv").versions.to_dataframe()
# uid                    key           is_latest
# tCUkRcaEjTjhtozp0000  results.csv   False
# tCUkRcaEjTjhtozp0001  results.csv   True
```

**Why**: LaminDB treats same-key uploads as version increments. There's no concept of "overwrite" or "replace at this path" — every new hash at the same key creates a version.

**What filesystem users expect**: Saving to the same path overwrites the file. No version chain. No version 2.

**The silent version substitution**: If you pass `revises=old_artifact` but `old_artifact` isn't the latest version, LaminDB **silently swaps it** for the latest version and only logs a warning.

---

## Pitfall 8: "I Registered an Existing File and Now I Can't Touch It"

**When you register a file already in your storage root:**
```python
# File exists at: s3://my-bucket/raw-data/experiment.csv
artifact = ln.Artifact("s3://my-bucket/raw-data/experiment.csv").save()
# _key_is_virtual = False — the key IS the real path
```

Now LaminDB considers this file "managed." But:
- You can't rename it via LaminDB (keys are quasi-immutable)
- If you rename it via S3 directly, the artifact record is now broken (points to nonexistent path)
- If another tool writes to the same path, LaminDB doesn't know
- Deleting the artifact may or may not delete the S3 object (prompts for confirmation via stdin — unusable in scripts)

**What filesystem users expect**: Registering a file is non-destructive and reversible. The file stays where it is, under their control.

---

## Pitfall 9: "Collections Are Not Folders"

Users naturally reach for `Collection` thinking it's LaminDB's folder equivalent:

```python
# This looks like creating a folder:
collection = ln.Collection([a1, a2, a3], key="my-folder").save()

# But collections don't behave like folders:
collection.artifacts.add(a4)     # OK — but a4 doesn't "move into" the folder
a1.collections.all()             # a1 can be in MULTIPLE collections simultaneously
collection.key                   # "my-folder" — but a1.key is still "totally-different-path.csv"
```

**Key differences from folders:**
- An artifact can be in multiple collections (many-to-many)
- Adding to a collection doesn't change the artifact's key or location
- Collections don't form hierarchies (no sub-collections)
- Deleting a collection doesn't delete its artifacts
- There's no "list files in this collection by subfolder"

**What filesystem users expect**: A folder contains files. A file is in one folder. Moving a file to another folder changes its location.

---

## Pitfall 10: "I Can't Do Glob Patterns on My Keys"

```python
# These don't work:
ln.Artifact.filter(key="experiments/*/results.csv")      # No glob support
ln.Artifact.filter(key__glob="experiments/*/results.csv") # Doesn't exist

# You have to decompose into multiple queries:
ln.Artifact.filter(key__startswith="experiments/", key__endswith="/results.csv")
# But this also matches "experiments/foo/bar/baz/results.csv"

# Or use regex (if you know Django ORM):
ln.Artifact.filter(key__regex=r"^experiments/[^/]+/results\.csv$")
```

**What filesystem users expect**: `glob("experiments/*/results.csv")` just works. It's fundamental to every filesystem workflow.

---

## Pitfall 11: "Hash Collisions Resurrect Deleted Files with Wrong Metadata"

```python
a = ln.Artifact("data.csv", key="correct-name.csv", description="Good data").save()
a.delete()           # Moved to trash (branch_id = -1)

# Later, upload identical file:
b = ln.Artifact("data.csv", key="better-name.csv", description="New experiment").save()
# b IS a — the trashed record was resurrected
# b.key = "correct-name.csv"          ← NOT "better-name.csv"
# b.description = "Good data"          ← NOT "New experiment"
```

Your new metadata is silently discarded. The zombie record returns with its old identity.

**What filesystem users expect**: A new upload creates a new file, regardless of content similarity to deleted files.

---

## Pitfall 12: "Virtual vs Non-Virtual Keys Change Based on Context"

The `_key_is_virtual` flag is set automatically based on how the artifact was created:

| Creation Method | `_key_is_virtual` | Actual Storage Path |
|---|---|---|
| `Artifact("local.csv", key="data.csv")` | `True` | `.lamindb/{uid}.csv` |
| `Artifact("s3://bucket/data.csv")` | `False` | `s3://bucket/data.csv` |
| `Artifact(df, key="data.parquet")` | `True` | `.lamindb/{uid}.parquet` |
| File already in storage root | `False` | Original location |

**Problem**: The same `key="data.csv"` means different things depending on where the source file lives. Users don't control (or even know about) this flag.

**Consequences**:
- `.path` returns different things for virtual vs non-virtual keys
- Deletion behavior differs (auto-delete storage vs prompt)
- Key mutability differs
- Two artifacts with the same `key` might be stored in completely different ways

---

## Summary: What a "Filesystem Compatibility Layer" Would Need

| Filesystem Operation | LaminDB Today | What's Missing |
|---|---|---|
| `ls path/` | `filter(key__startswith=...)` | Dedicated `.ls()` method |
| `tree path/` | Nothing | Tree visualization from keys |
| `mv old new` | Create new version (workaround) | Key rename operation |
| `cp src dst` | `skip_hash_lookup=True` | Explicit copy-with-new-key |
| `rm file` | Two-stage delete | Single-step permanent delete option |
| `glob *.csv` | `key__regex` (Django knowledge needed) | `.glob()` method on QuerySet |
| `find -name X` | `filter(key__contains=X)` | Close enough, but no recursion depth control |
| `du -sh folder/` | Nothing | Storage size by key prefix |
| `ln -s` (symlink) | Nothing | Multiple keys → same artifact |
| `stat file` | `.describe()` | Decent, but not discoverable |

### Recommended Additions

1. **`ln.Artifact.ls("prefix/")`** — List artifacts under a key prefix, showing only the next "level" (like `ls`, not `find`)
2. **`ln.Artifact.tree("prefix/")`** — Rich tree rendering of key hierarchy
3. **`artifact.move("new/key")`** — Rename key without creating a version chain
4. **`ln.Artifact.glob("experiments/*/results.csv")`** — Glob pattern matching on keys
5. **`artifact.copy(key="new/key")`** — Explicit duplicate with new key, skipping dedup
6. **Multi-key aliases** — Allow an artifact to have multiple keys pointing to the same storage, enabling folder-like reorganization without duplication
7. **Explain virtual keys at onboarding** — Most confusion stems from not knowing `.lamindb/{uid}` exists
8. **Consistent delete semantics** — Either always soft-delete or always hard-delete; don't mix based on call count
