from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

import pandas as pd
from lamindb_setup import logger
from lamindb_setup.core.canonical_suffix import CanonicalSuffix
from lamindb_setup.core.hashing import hash_dir
from lamindb_setup.core.upath import LocalPathClasses, create_path

from lamindb.core.loaders import load_to_memory
from lamindb.errors import InvalidArgument, ValidationError
from lamindb.models.artifact import Artifact
from lamindb.models.schema import validate_folder_slots

from .core import (
    AnnDataCurator,
    DataFrameCurator,
    ExperimentalDictCurator,
    MuDataCurator,
    SlotsCurator,
    SpatialDataCurator,
    TiledbsomaExperimentCurator,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from lamindb_setup.types import AnyPathStr
    from upath import UPath

    from lamindb.models.run import Run
    from lamindb.models.schema import Schema


@contextmanager
def _open_child(path: Path, otype: str | None) -> Iterator[Any]:
    if otype == "tiledbsoma":
        import tiledbsoma

        with tiledbsoma.Experiment.open(str(path), mode="r") as experiment:
            yield experiment
    else:
        yield load_to_memory(path)


def _check_cache_matches_cloud(cloud: UPath, local: Path) -> None:
    # mtimes are not comparable: after upload, lamindb stamps cached files newer
    cloud_files = {
        PurePosixPath(key).relative_to(cloud.path).as_posix(): info["size"]
        for key, info in cloud.fs.find(str(cloud), detail=True).items()
        if not key.endswith("/")
    }
    local_files = {
        path.relative_to(local).as_posix(): path.stat().st_size
        for path in local.rglob("*")
        if path.is_file()
    }
    if cloud_files != local_files:
        differing = sorted(
            {*cloud_files.items()}.symmetric_difference(local_files.items())
        )
        raise ValidationError(
            f"Cached folder {local} differs from {cloud} at '{differing[0][0]}'. "
            "Delete the cached folder and retry."
        )


def _check_folder_otype(artifact: Artifact) -> None:
    if artifact.otype not in {None, "Folder"}:
        raise InvalidArgument(
            f"Cannot curate artifact with otype {artifact.otype!r} as a folder."
        )


class FolderCurator(SlotsCurator):
    """Validate objects inside a local directory against path-keyed schema slots.

    Each slot is a required relative path. Child schemas select the curator by
    object type; suffixes constrain storage formats independently.

    Args:
        dataset: A local directory path or a folder artifact. Cloud artifacts
            are synced to the local cache.
        schema: A saved schema with `otype="Folder"` and path-keyed slots.

    Example:
        .. literalinclude:: scripts/curate_folder.py
            :language: python
    """

    def __init__(self, dataset: AnyPathStr | Artifact, schema: Schema) -> None:
        artifact = dataset if isinstance(dataset, Artifact) else None
        if artifact is None:
            root = create_path(dataset)
        else:
            _check_folder_otype(artifact)
            root = create_path(
                getattr(artifact, "_local_filepath", None) or artifact.path
            )
            if not root.is_dir():
                raise InvalidArgument(
                    f"Artifact {artifact.uid} is not a folder or no longer exists."
                )
            if not isinstance(root, LocalPathClasses):
                cloud = root
                root = artifact.cache(is_run_input=False)
                _check_cache_matches_cloud(cloud, Path(root))
        if not isinstance(root, LocalPathClasses):
            raise InvalidArgument("Folder curation requires a local directory.")
        super().__init__(dataset=root, schema=schema)
        if schema.otype != "Folder":
            raise InvalidArgument("Schema otype must be 'Folder'.")
        self._root: Path = Path(root)
        self._artifact = artifact
        self._validated_stats: tuple[int, str, str, int] | None = None

    def validate(self) -> None:
        self._is_validated = False
        self._slots = {}
        validate_folder_slots(self._schema.slots)
        if (
            self._schema.otype != "Folder"
            or not self._schema.minimal_set
            or self._schema.ordered_set
            or self._schema.coerce
            or self._schema.itype is not None
            or self._schema.n_members
        ):
            raise InvalidArgument(
                "Folder schemas require otype='Folder', minimal_set=True, "
                "ordered_set=False, and no features or coercion."
            )
        if self._root.is_symlink():
            raise InvalidArgument("Folder validation does not support symlinks.")
        if not self._root.is_dir():
            raise ValidationError(f"Folder does not exist: {self._root}")
        for path in self._root.rglob("*"):
            if path.is_symlink():
                raise InvalidArgument(
                    f"Folder validation does not support symlinks: {path}"
                )
            if self._schema.maximal_set and path.is_file():
                relative = path.relative_to(self._root).as_posix()
                if not any(
                    relative == slot or relative.startswith(f"{slot}/")
                    for slot in self._schema.slots
                ):
                    raise ValidationError(f"Undeclared file: {relative}")
        for slot in self._schema.slots:
            if not (self._root / slot).exists():
                raise ValidationError(f"Missing required object: {slot}")
        # hash before parsing so that edits made during validation are detected on save
        validated_stats = hash_dir(self._root)
        for slot, schema in self._schema.slots.items():
            path = self._root / slot
            suffix = CanonicalSuffix.extract_from_path(path)[1]
            if schema.suffix is not None and suffix != schema.suffix:
                raise ValidationError(
                    f"{slot}: suffix '{suffix}' does not match '{schema.suffix}'."
                )
            curator_type = {
                None: DataFrameCurator,
                "DataFrame": DataFrameCurator,
                "AnnData": AnnDataCurator,
                "MuData": MuDataCurator,
                "dict": ExperimentalDictCurator,
                "SpatialData": SpatialDataCurator,
                "tiledbsoma": TiledbsomaExperimentCurator,
            }.get(schema.otype)
            if curator_type is None:
                raise InvalidArgument(
                    f"{slot}: unsupported child otype: {schema.otype!r}"
                )
            try:
                with _open_child(path, schema.otype) as dataset:
                    if schema.otype in {None, "DataFrame"} and not isinstance(
                        dataset, pd.DataFrame
                    ):
                        raise InvalidArgument("Expected a DataFrame object.")
                    child = (
                        ExperimentalDictCurator(dataset, schema, validate_keys=True)
                        if schema.otype == "dict"
                        else curator_type(dataset, schema)
                    )
                    components = dict(child.slots)
                    if isinstance(child, DataFrameCurator):
                        components["columns"] = child._atomic_curator
                    if any(
                        component._coercion_requested
                        for component in components.values()
                    ):
                        raise InvalidArgument(
                            "Folder validation is read-only; coercion is not supported."
                        )
                    child.validate()
            except (ValidationError, InvalidArgument) as error:
                raise type(error)(f"{slot}: {error}") from error
            except Exception as error:
                if hasattr(error, "add_note"):  # Python >= 3.11
                    error.add_note(f"folder slot: {slot}")
                raise
            if isinstance(child, DataFrameCurator):
                self._slots[slot] = child._atomic_curator
            for child_slot, component in child.slots.items():
                self._slots[f"{slot}::{child_slot}"] = component
        self._validated_stats = validated_stats
        self._is_validated = True

    def _prepare_artifact(
        self,
        *,
        key: str | None = None,
        description: str | None = None,
        revises: Artifact | None = None,
        run: Run | None = None,
    ) -> Artifact:
        if not self._is_validated:
            self.validate()
        artifact = self._artifact
        if artifact is None:
            stats_keys = ("size", "hash", "hash_type", "n_files")
            artifact = Artifact(
                self._root,
                key=key,
                description=description,
                revises=revises,
                run=run,
                kind="dataset",
                _stats=dict(zip(stats_keys, self._validated_stats, strict=True)),
            )
            # the constructor may return an existing artifact with the same hash
            _check_folder_otype(artifact)
        elif any(arg is not None for arg in (key, description, revises, run)):
            logger.warning(
                "ignoring key, description, revises and run for an existing artifact"
            )
        artifact.otype = "Folder"
        artifact.schema = self._schema
        artifact._folder_curator = self
        return artifact

    def _annotate_artifact(self, artifact: Artifact) -> None:
        from .core import annotate_artifact

        cat_vectors = {
            f"{slot}::{key}": vector
            for slot, component in self._slots.items()
            for key, vector in component.cat._cat_vectors.items()
            if key not in {"columns", "var_index"}
        }
        annotate_artifact(artifact, curator=self, cat_vectors=cat_vectors)
        self._artifact = artifact

    def _validate_for_save(self, artifact: Artifact) -> None:
        has_files = any(path.is_file() for path in self._root.rglob("*"))
        current_hash = hash_dir(self._root)[1] if has_files else None
        if not self._is_validated or current_hash != self._validated_stats[1]:
            self.validate()
        # existing cloud artifacts are hashed from etags, so only compare new ones
        if artifact._state.adding and artifact.hash not in {None, current_hash}:
            raise ValidationError(
                "Folder content changed after artifact preparation. "
                "Create a new Artifact.from_folder() from the current content."
            )

    def save_artifact(
        self,
        *,
        key: str | None = None,
        description: str | None = None,
        revises: Artifact | None = None,
        run: Run | None = None,
    ) -> Artifact:
        """Validate current content and save one annotated folder artifact."""
        return self._prepare_artifact(
            key=key, description=description, revises=revises, run=run
        ).save()
