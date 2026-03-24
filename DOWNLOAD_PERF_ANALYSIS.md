# Artifact downloads are slower than native S3 — missing fsspec/boto3 tuning

## Problem

Artifact downloads (`artifact.cache()`, `artifact.load()`) use default fsspec/s3fs settings with no performance tuning. For large files, this can be **5-10x slower** than a properly configured native S3 transfer.

## Root Cause Analysis

### 1. No transfer configuration propagated to downloads

Uploads accept `**kwargs` that flow through to `UPath.upload_from()`, allowing custom transfer settings:

```python
# paths.py:167 — uploads get kwargs
storage_path.upload_from(local_path, create_folder=create_folder,
                         print_progress=print_progress, **kwargs)
```

Downloads do **not** propagate any such kwargs:

```python
# artifact.py — downloads get no tuning knobs
setup_settings.paths.cloud_to_local(filepath, cache_key=cache_key,
                                     print_progress=print_progress)
```

This asymmetry means uploads can be optimized but downloads cannot.

### 2. Default fsspec/s3fs settings are conservative

Without explicit configuration, s3fs uses:
- **~5 MB block size** (vs. boto3's 8 MB default multipart threshold)
- **Limited concurrency** (~1-4 connections vs. boto3's default 10)
- **No multipart download acceleration** for large files

A native boto3 transfer with `TransferConfig(max_concurrency=10, multipart_threshold=8*1024*1024)` significantly outperforms the defaults.

### 3. Potential double-download in `artifact.load()`

`artifact.load()` calls `cloud_to_local()` in `_synchronize_cleanup_on_error()`, and then some loaders in `load_to_memory()` call `cloud_to_local()` again. While the second call should hit cache, the cache freshness check still adds overhead.

## Proposed Solutions

### Short-term: Propagate transfer kwargs through download path

Allow `cloud_to_local()` to accept and forward transfer configuration:

```python
# In artifact.cache() / artifact.load()
setup_settings.paths.cloud_to_local(
    filepath, cache_key=cache_key,
    print_progress=print_progress,
    block_size=0,            # stream entire file
    max_concurrency=10,      # parallel chunk downloads
    **kwargs
)
```

### Medium-term: Configure s3fs at the storage level

Add configurable transfer settings per `StorageSettings`:

```python
# Example: set once, applied to all operations on this storage
storage.transfer_config = {
    "max_concurrency": 10,
    "multipart_threshold": 8 * 1024 * 1024,
    "multipart_chunksize": 8 * 1024 * 1024,
}
```

This would be passed to s3fs via `client_kwargs` or `config_kwargs` in the storage options.

### Long-term: Use boto3 TransferConfig directly for S3

For S3 specifically, bypass fsspec's download path and use boto3's managed transfer directly:

```python
import boto3
from boto3.s3.transfer import TransferConfig

config = TransferConfig(
    max_concurrency=10,
    multipart_threshold=8 * 1024 * 1024,
    multipart_chunksize=8 * 1024 * 1024,
)
s3 = boto3.client("s3")
s3.download_file(bucket, key, local_path, Config=config)
```

This gives full control over concurrency and multipart behavior, and is the fastest path for S3.

## Impact

- Large file downloads (>100 MB) would see the biggest improvement
- Collection downloads with many files would benefit from concurrent transfers
- Cross-region downloads benefit most from higher concurrency

## Files involved

- `lamindb/models/artifact.py` — `cache()`, `load()`, `_synchronize_cleanup_on_error()`
- `lamindb/core/storage/paths.py` — `store_file_or_folder()`
- `lamindb/core/loaders.py` — `load_to_memory()` calls `cloud_to_local()`
- `lamindb_setup` (external) — `cloud_to_local()` implementation
