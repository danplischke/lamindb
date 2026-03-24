# Artifact downloads are slower than native S3 — fsspec analysis & proposed fixes

## Problem

Artifact downloads (`artifact.cache()`, `artifact.load()`) use default fsspec/s3fs settings with no performance tuning. For large files, this can be **5-10x slower** than a properly configured native S3 transfer (boto3 with `TransferConfig`).

---

## How fsspec Downloads Actually Work

### Full-file download: `cloud_to_local` → fsspec `get_file`

When `artifact.cache()` triggers `cloud_to_local()`, the chain is:

```
artifact.cache()
  → _synchronize_cleanup_on_error()           # artifact.py:3156
    → setup_settings.paths.cloud_to_local()   # lamindb_setup
      → UPath.synchronize_to()                # upath/lamindb_setup
        → fsspec AbstractFileSystem.get_file() # fsspec
          → single streaming GET request       # aiobotocore/aiohttp
```

**Key behavior of `get_file`:**
- Opens a **single HTTP GET** for the entire file
- Streams to disk sequentially — no multipart, no parallelism
- One TCP connection, one thread
- No `max_concurrency` or `multipart_threshold` parameters

Compare to boto3's managed transfer:
- Splits files >8 MB into chunks
- Downloads chunks across **10 concurrent threads** by default
- Uses HTTP range requests for parallel multipart download

### Streaming/random-access: `fs.open` → `AbstractBufferedFile`

When `artifact.open()` opens a file for streaming (e.g., h5py), fsspec uses `AbstractBufferedFile`:

```
artifact.open()
  → fs.open(path, mode="rb")                  # _anndata_accessor.py:179
    → S3File (extends AbstractBufferedFile)
      → reads in block_size chunks (default: 5 MB)
      → cache_type controls prefetch strategy
```

**Cache types** (controls how blocks are fetched and retained):

| `cache_type` | Behavior | Good for |
|--------------|----------|----------|
| `"bytes"` (default) | LRU cache of fetched 5 MB blocks | Small random reads |
| `"readahead"` | Prefetches next block automatically | Sequential streaming |
| `"all"` | Downloads entire file on first read | Small files only |
| `"none"` | No caching, every `read()` hits network | Never |

**Problem**: lamindb uses the default (`"bytes"` cache, 5 MB blocks) for all access patterns, which means:
- Sequential H5AD reads do many 5 MB GETs instead of prefetching
- Random access on large files causes excessive round trips (each ~50-100ms latency)

### s3fs connection internals

s3fs uses aiobotocore (async botocore) with aiohttp:
- Default connection pool is small (~5 connections)
- No explicit connection pool tuning in lamindb or lamindb_setup
- Each `fs.connect()` call (done in `_polars_lazy_df.py:23`, `_duckdb_relation.py:20`) creates session state but doesn't configure pool size

---

## Specific Bottlenecks (with code references)

### 1. Single-threaded full-file download — HIGH IMPACT

**Where**: `cloud_to_local()` → fsspec `get_file()` (called from `artifact.py:3161`)

**Why slow**: One TCP connection, no multipart, no parallelism. For a 1 GB file on a 1 Gbps link:
- fsspec: ~1 connection → ~8-10 seconds theoretical minimum
- boto3 with 10 threads: ~1-2 seconds

**Expected improvement**: 3-10x for files >100 MB.

### 2. `load_to_memory` drops kwargs — MEDIUM IMPACT

**Where**: `lamindb/core/loaders.py:205`
```python
filepath = setup_settings.paths.cloud_to_local(filepath, print_progress=True)
```

`artifact.load()` passes `**kwargs` through `_synchronize_cleanup_on_error`, but when `load_to_memory` is called, it makes its own `cloud_to_local()` call with **no kwargs**. Any transfer tuning is lost on this path.

### 3. Default 5 MB block_size for streaming — MEDIUM IMPACT

**Where**: `lamindb/core/storage/_anndata_accessor.py:179`
```python
conn = fs.open(file_path_str, mode=conn_mode, compression=compression)
```

No `block_size` or `cache_type` specified. For H5AD files with random access patterns (reading specific `.obs` columns), each block fetch is a separate S3 GET with ~50-100ms latency overhead. With 5 MB blocks, reading scattered metadata requires many round trips.

### 4. Redundant HEAD request in `load()` — LOW IMPACT

**Where**: `artifact.load()` calls `cloud_to_local()` twice:
1. `_synchronize_cleanup_on_error()` at `artifact.py:3161` — downloads the file
2. `load_to_memory()` at `loaders.py:205` — calls `cloud_to_local()` again

The second call hits cache but still issues a HEAD request to S3 to verify freshness (~50-100ms). Since `load()` just downloaded the file moments ago, this check is wasted.

### 5. No connection pool tuning — LOW-MEDIUM IMPACT

**Where**: Nowhere — no `config_kwargs` or `client_kwargs` passed to s3fs.

For workloads accessing many files (e.g., loading a Collection), the small default connection pool means connections are created/destroyed repeatedly instead of being reused.

### 6. Zarr store creation doesn't tune async concurrency

**Where**: `lamindb/core/storage/_zarr.py:43`
```python
store = zarr.storage.FsspecStore.from_upath(UPath(storepath, asynchronous=True))
```

Zarr v3 FsspecStore supports async but inherits fsspec's default concurrency. No `max_concurrency` is passed to the underlying filesystem.

---

## Proposed Fixes (Priority Order)

### Fix 1: Skip redundant `cloud_to_local` in `load_to_memory` — QUICK WIN

Since `artifact.load()` already downloads via `_synchronize_cleanup_on_error`, pass the **local cache path** to `load_to_memory` instead of the cloud path. This eliminates the redundant HEAD request entirely.

```python
# artifact.py — in load(), after _synchronize_cleanup_on_error returns cache_path
return load_to_memory(cache_path, **kwargs)  # pass local path, not cloud path
```

**Impact**: Saves 50-100ms per `load()` call. Zero risk.

### Fix 2: Propagate `**kwargs` through `load_to_memory` — QUICK WIN

```python
# loaders.py:205 — pass kwargs through
def load_to_memory(filepath: UPathStr, **kwargs) -> ...:
    # ...
    filepath = setup_settings.paths.cloud_to_local(filepath, print_progress=True, **kwargs)
```

**Impact**: Enables users to pass transfer tuning to the full download path.

### Fix 3: Tune streaming `block_size` and `cache_type` — LOW EFFORT

```python
# _anndata_accessor.py:179
block_size = 50 * 2**20 if not isinstance(fs, LocalFileSystem) else 0
conn = fs.open(file_path_str, mode=conn_mode, compression=compression,
               block_size=block_size, cache_type="readahead")
```

**Impact**: Reduces round trips for sequential reads by 10x. Larger blocks also amortize per-request latency.

### Fix 4: Configure s3fs connection pool — LOW EFFORT

In lamindb_setup's storage initialization, pass botocore config:

```python
fs = s3fs.S3FileSystem(
    config_kwargs={"max_pool_connections": 25},
    client_kwargs={"read_timeout": 300},
)
```

**Impact**: Better connection reuse for workloads with many files.

### Fix 5: Use boto3 managed transfer for full-file S3 downloads — MEDIUM EFFORT, HIGH IMPACT

For S3 paths specifically, bypass fsspec's `get_file` and use boto3 directly:

```python
from boto3.s3.transfer import TransferConfig

config = TransferConfig(
    max_concurrency=10,
    multipart_threshold=8 * 1024 * 1024,
    multipart_chunksize=8 * 1024 * 1024,
)

# Extract credentials from s3fs filesystem object
fs = s3_path.fs
client = boto3.client("s3",
    endpoint_url=fs.endpoint_url,
    aws_access_key_id=fs.key,
    aws_secret_access_key=fs.secret,
    aws_session_token=fs.token,
)
bucket, key = parse_s3_uri(s3_path)
client.download_file(bucket, key, str(local_path), Config=config)
```

**Impact**: 3-10x faster for files >100 MB. This is the single highest-impact change.

**Note**: Would need to live in `lamindb_setup` since `cloud_to_local` is implemented there, or as an override in lamindb that intercepts S3 paths before they reach `cloud_to_local`.

### Fix 6: Per-storage transfer configuration — MEDIUM EFFORT

Add `transfer_config` to `StorageSettings` so settings are applied consistently:

```python
storage.transfer_config = {
    "max_concurrency": 10,
    "multipart_threshold": 8 * 1024 * 1024,
}
```

Passed to s3fs via `config_kwargs` in storage options, affecting all operations on that storage.

---

## Upload/Download Asymmetry Summary

| Aspect | Upload | Download |
|--------|--------|----------|
| **Accepts `**kwargs`** | Yes (`upload_from` at `paths.py:167`) | Partially (`_synchronize_cleanup_on_error` yes, `load_to_memory` no) |
| **Parallelism configurable** | Via kwargs to UPath | No — fsspec `get_file` is single-stream |
| **Block size tunable** | N/A (full file) | No — `fs.open()` uses defaults |
| **Connection pool** | Default | Default |

---

## Files Involved

| File | Role | Issue |
|------|------|-------|
| `lamindb/models/artifact.py:3156-3174` | `_synchronize_cleanup_on_error` | Passes kwargs ✓ |
| `lamindb/models/artifact.py:2849-2924` | `load()` | Passes cloud path to `load_to_memory` instead of cache path |
| `lamindb/core/loaders.py:205` | `load_to_memory` | Drops kwargs, makes redundant `cloud_to_local` call |
| `lamindb/core/storage/_anndata_accessor.py:179` | H5py streaming open | No `block_size` / `cache_type` |
| `lamindb/core/storage/_zarr.py:43` | Zarr store | No async concurrency config |
| `lamindb/core/storage/_polars_lazy_df.py:23` | Polars S3 | `fs.connect()` with no pool tuning |
| `lamindb/core/storage/_duckdb_relation.py:20` | DuckDB S3 | `fs.connect()` with no pool tuning |
| `lamindb/core/storage/paths.py:167` | Upload path | Properly passes kwargs ✓ |
| `lamindb_setup` (external) | `cloud_to_local()` impl | Uses fsspec defaults |
