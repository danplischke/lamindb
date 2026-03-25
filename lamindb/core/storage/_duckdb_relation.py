from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from lamindb_setup.core.upath import _ensure_sync_with_fs, get_storage_region

if TYPE_CHECKING:
    from collections.abc import Iterator

    from duckdb import DuckDBPyConnection, DuckDBPyRelation
    from upath import UPath

DUCKDB_SUFFIXES = (".parquet", ".csv", ".json", ".ndjson")


def _configure_s3(conn: DuckDBPyConnection, storepath: UPath) -> None:
    fs = storepath.fs
    fs.connect()

    endpoint_url = fs.endpoint_url
    if endpoint_url is not None:
        # strip protocol prefix for duckdb
        endpoint = endpoint_url.replace("https://", "").replace("http://", "")
        conn.execute(f"SET s3_endpoint='{endpoint}'")
        conn.execute("SET s3_url_style='path'")
        if endpoint_url.startswith("http://"):
            conn.execute("SET s3_use_ssl=false")
    else:
        region = get_storage_region(storepath)
        conn.execute(f"SET s3_region='{region}'")

    if fs.anon:
        return

    aws_key = fs.key
    aws_secret = fs.secret
    aws_token = fs.token
    if aws_key is not None and aws_secret is not None:
        conn.execute(f"SET s3_access_key_id='{aws_key}'")
        conn.execute(f"SET s3_secret_access_key='{aws_secret}'")
        if aws_token is not None:
            conn.execute(f"SET s3_session_token='{aws_token}'")
    else:
        from aiobotocore.credentials import AioRefreshableCredentials

        if isinstance(
            refreshable_credentials := fs.session._credentials,
            AioRefreshableCredentials,
        ):
            refresh_sync = _ensure_sync_with_fs(
                refreshable_credentials._refresh, fs
            )
            # refresh and set the current credentials
            refresh_sync()
            conn.execute(
                f"SET s3_access_key_id='{refreshable_credentials._access_key}'"
            )
            conn.execute(
                f"SET s3_secret_access_key='{refreshable_credentials._secret_key}'"
            )
            if refreshable_credentials._token is not None:
                conn.execute(
                    f"SET s3_session_token='{refreshable_credentials._token}'"
                )


def _configure_gcs(conn: DuckDBPyConnection, storepath: UPath) -> None:
    fs = storepath.fs

    # get an OAuth2 access token from gcsfs credentials
    credentials = getattr(fs, "credentials", None)
    if credentials is not None:
        token = getattr(credentials, "token", None)
        if token is not None:
            conn.execute(f"SET s3_access_key_id='{token}'")

    # GCS is accessed via S3-compatible endpoint
    conn.execute("SET s3_endpoint='storage.googleapis.com'")
    conn.execute("SET s3_url_style='path'")


@contextmanager
def _open_duckdb_relation(
    paths: UPath | list[UPath],
    conn: DuckDBPyConnection | None = None,
    **kwargs,
) -> Iterator[DuckDBPyRelation]:
    """Open paths as a DuckDB relation.

    Args:
        paths: One or more UPath objects pointing to data files.
        conn: An existing DuckDB connection to use. If ``None``, an ephemeral
            in-memory connection is created and closed when the context manager
            exits. If provided, the caller owns the connection and it will
            **not** be closed on exit.
        **kwargs: Passed to the underlying ``read_parquet``/``read_csv``/``read_json`` call.
    """
    try:
        import duckdb
    except ImportError as ie:
        raise ImportError("Please install duckdb: pip install duckdb") from ie

    path_list = []
    if isinstance(paths, Path):
        paths = [paths]
    for path in paths:
        # assume http is always a file
        if getattr(path, "protocol", None) not in {"http", "https"} and path.is_dir():
            path_list += [p for p in path.rglob("*") if p.suffix != ""]
        else:
            path_list.append(path)

    # assume the filesystem is the same for all
    # it is checked in _open_dataframe
    path0 = path_list[0]
    protocol = getattr(path0, "protocol", "file")

    owns_conn = conn is None
    if owns_conn:
        conn = duckdb.connect()
    try:
        if protocol == "s3":
            conn.execute("INSTALL httpfs; LOAD httpfs")
            _configure_s3(conn, path0)
        elif protocol in {"gs", "gcs"}:
            conn.execute("INSTALL httpfs; LOAD httpfs")
            _configure_gcs(conn, path0)

        path_strs = [p.as_posix() for p in path_list]
        source = path_strs[0] if len(path_strs) == 1 else path_strs
        suffix = path_list[0].suffix

        if suffix == ".parquet":
            yield conn.read_parquet(source, **kwargs)
        elif suffix == ".csv":
            yield conn.read_csv(source, **kwargs)
        elif suffix in {".json", ".ndjson"}:
            yield conn.read_json(source, **kwargs)
        else:
            raise ValueError(
                f"DuckDB does not support {suffix} files, "
                f"they should have one of these formats: {', '.join(DUCKDB_SUFFIXES)}."
            )
    finally:
        if owns_conn:
            conn.close()
