import anndata as ad
import lamindb as ln
import numpy as np
import pandas as pd
import pytest


@pytest.fixture(autouse=True)
def clean_folder_records():
    from lamindb.models.schema import SchemaComponent

    registries = (ln.Artifact, ln.Schema, ln.Feature, ln.ULabel)
    before = {
        registry: set(registry.objects.values_list("id", flat=True))
        for registry in registries
    }
    yield
    for registry in registries:
        created = registry.objects.exclude(id__in=before[registry])
        if registry is ln.Schema:
            SchemaComponent.objects.filter(composite__in=created).delete()
        created.delete()


@pytest.fixture
def folder_input(tmp_path):
    root = tmp_path / "study"
    root.mkdir()
    pd.DataFrame({"folder_sample_id": ["s1", "s2"]}).to_parquet(
        root / "samples.parquet", index=False
    )
    sample_id = ln.Feature(name="folder_sample_id", dtype=str).save()
    samples = ln.Schema(
        features=[sample_id], otype="DataFrame", suffix=".parquet"
    ).save()
    schema = ln.Schema(
        otype="Folder", slots={"samples.parquet": samples}, maximal_set=True
    ).save()
    return root, schema


def test_folder_validate(folder_input):
    root, schema = folder_input
    before = ln.Artifact.objects.count()
    curator_type = getattr(ln.curators, "FolderCurator", None)
    assert curator_type is not None
    curator = curator_type(root, schema)
    assert curator.validate() is None
    assert ln.Artifact.objects.count() == before


@pytest.mark.parametrize("suffix", [".h5ad", ".zarr"])
def test_folder_composes_table_and_anndata(folder_input, suffix):
    root, schema = folder_input
    obs_feature = ln.Feature(name="folder_group", dtype=str).save()
    obs_schema = ln.Schema(features=[obs_feature]).save()
    adata_schema = ln.Schema(
        otype="AnnData", slots={"obs": obs_schema}, suffix=suffix
    ).save()
    adata = ad.AnnData(
        X=np.ones((2, 2)),
        obs=pd.DataFrame({"folder_group": ["a", "b"]}, index=["s1", "s2"]),
    )
    path = root / f"expression{suffix}"
    if suffix == ".zarr":
        adata.write_zarr(path)
    else:
        adata.write_h5ad(path)
    mixed = ln.Schema(
        otype="Folder",
        slots={**schema.slots, path.name: adata_schema},
        maximal_set=True,
    ).save()
    curator = ln.curators.FolderCurator(root, mixed)
    assert curator.validate() is None
    assert set(curator.slots) == {"samples.parquet", f"{path.name}::obs"}


def test_folder_schema_identity(folder_input):
    _, schema = folder_input
    other = ln.Schema(otype="OtherComposite", slots=schema.slots, maximal_set=True)
    assert other.hash != schema.hash


@pytest.mark.parametrize(
    "slot",
    [
        "",
        "/samples.csv",
        "../samples.csv",
        "a/../samples.csv",
        "./samples.csv",
        "a//samples.csv",
        r"a\samples.csv",
        "s3://bucket/samples.csv",
        "*.csv",
        "__external__",
    ],
)
def test_folder_rejects_unsafe_slots(folder_input, slot):
    _, schema = folder_input
    child = schema.slots["samples.parquet"]
    with pytest.raises(ln.errors.InvalidArgument):
        ln.Schema(otype="Folder", slots={slot: child})


@pytest.mark.parametrize("strict", [False, True])
def test_folder_extra_files(folder_input, strict):
    root, schema = folder_input
    (root / "notes.txt").write_text("notes")
    schema = ln.Schema(otype="Folder", slots=schema.slots, maximal_set=strict).save()
    curator = ln.curators.FolderCurator(root, schema)
    if strict:
        with pytest.raises(ln.errors.ValidationError, match="notes.txt"):
            curator.validate()
    else:
        curator.validate()


def test_folder_invalid_content_has_path(folder_input):
    root, schema = folder_input
    pd.DataFrame({"wrong": [1]}).to_parquet(root / "samples.parquet")
    with pytest.raises(ln.errors.ValidationError, match="samples.parquet"):
        ln.curators.FolderCurator(root, schema).validate()


def test_folder_suffix_constraint(folder_input):
    root, schema = folder_input
    child = schema.slots["samples.parquet"]
    wrong_format = ln.Schema(features=list(child.members), suffix=".csv").save()
    schema = ln.Schema(otype="Folder", slots={"samples.parquet": wrong_format}).save()
    with pytest.raises(ln.errors.ValidationError, match="suffix"):
        ln.curators.FolderCurator(root, schema).validate()


def test_folder_symlinks_rejected(folder_input, tmp_path):
    root, schema = folder_input
    target = tmp_path / "outside.parquet"
    (root / "samples.parquet").rename(target)
    (root / "samples.parquet").symlink_to(target)
    with pytest.raises(ln.errors.InvalidArgument, match="symlink"):
        ln.curators.FolderCurator(root, schema).validate()


def test_folder_coercion_rejected(folder_input):
    root, schema = folder_input
    child = ln.Schema(
        features=list(schema.slots["samples.parquet"].members), coerce=True
    ).save()
    schema = ln.Schema(otype="Folder", slots={"samples.parquet": child}).save()
    with pytest.raises(ln.errors.InvalidArgument, match="coerc"):
        ln.curators.FolderCurator(root, schema).validate()


def test_folder_dict_child(folder_input):
    root, schema = folder_input
    (root / "metadata.json").write_text('{"folder_sample_id": "s1"}')
    child = ln.Schema(
        features=list(schema.slots["samples.parquet"].members),
        otype="dict",
        suffix=".json",
    ).save()
    schema = ln.Schema(
        otype="Folder", slots={**schema.slots, "metadata.json": child}
    ).save()
    artifact = ln.curators.FolderCurator(root, schema).save_artifact(
        key="folder-tests/dict"
    )
    assert artifact._links_schema.get(slot="metadata.json")


def test_folder_mudata_child(tmp_path):
    import mudata as md

    feature = ln.Feature(name="folder_modality", dtype=str).save()
    obs = ln.Schema(features=[feature]).save()
    schema = ln.Schema(otype="MuData", slots={"obs": obs}, suffix=".h5mu").save()
    data = md.MuData({"rna": ad.AnnData(np.ones((2, 2)))})
    data.obs["folder_modality"] = ["a", "b"]
    data.write_h5mu(tmp_path / "multi.h5mu")
    folder = ln.Schema(
        otype="Folder", slots={"multi.h5mu": schema}, maximal_set=True
    ).save()
    artifact = ln.curators.FolderCurator(tmp_path, folder).save_artifact(
        key="folder-tests/mudata"
    )
    assert artifact._links_schema.get(slot="multi.h5mu::obs")


def test_folder_constructor_and_annotations(folder_input):
    root, schema = folder_input
    before = ln.Artifact.objects.count()
    constructor = getattr(ln.Artifact, "from_folder", None)
    assert constructor is not None
    artifact = constructor(root, schema=schema, key="folder-tests/study")
    assert artifact._state.adding
    assert artifact.otype == "Folder"
    assert artifact.schema == schema
    assert ln.Artifact.objects.count() == before
    artifact.save()
    assert ln.Artifact.objects.count() == before + 1
    artifact = ln.Artifact.get(artifact.uid)
    assert artifact.schema == schema
    assert artifact.otype == "Folder"
    assert artifact.n_files == 1
    assert artifact._links_schema.get(slot="samples.parquet").schema.members.get(
        name="folder_sample_id"
    )
    ln.curators.FolderCurator(artifact, schema).validate()


def test_folder_save_revalidates(folder_input):
    root, schema = folder_input
    curator = ln.curators.FolderCurator(root, schema)
    curator.validate()
    (root / "samples.parquet").unlink()
    with pytest.raises(ln.errors.ValidationError, match="samples.parquet"):
        curator.save_artifact(key="folder-tests/changed")
    assert not ln.Artifact.filter(key="folder-tests/changed").exists()


def test_folder_constructor_save_revalidates(folder_input):
    root, schema = folder_input
    constructor = getattr(ln.Artifact, "from_folder", None)
    assert constructor is not None
    artifact = constructor(root, schema=schema, key="folder-tests/prepared")
    pd.DataFrame({"wrong": [1]}).to_parquet(root / "samples.parquet")
    with pytest.raises(ln.errors.ValidationError, match="samples.parquet"):
        artifact.save()
    assert not ln.Artifact.filter(key="folder-tests/prepared").exists()


def test_folder_save_nested_annotations(folder_input):
    root, schema = folder_input
    obs_feature = ln.Feature(name="folder_nested_obs", dtype=str).save()
    var_feature = ln.Feature(name="folder_nested_var", dtype=str).save()
    obs_schema = ln.Schema(features=[obs_feature]).save()
    var_schema = ln.Schema(features=[var_feature]).save()
    adata_schema = ln.Schema(
        otype="AnnData", slots={"obs": obs_schema, "var": var_schema}
    ).save()
    adata = ad.AnnData(
        X=np.ones((2, 2)),
        obs=pd.DataFrame({"folder_nested_obs": ["a", "b"]}, index=["s1", "s2"]),
        var=pd.DataFrame({"folder_nested_var": ["p1", "p2"]}, index=["v1", "v2"]),
    )
    adata.write_zarr(root / "expression.zarr")
    mixed = ln.Schema(
        otype="Folder",
        slots={**schema.slots, "expression.zarr": adata_schema},
        maximal_set=True,
    ).save()
    before = ln.Artifact.objects.count()
    artifact = ln.curators.FolderCurator(root, mixed).save_artifact(
        key="folder-tests/mixed"
    )
    assert ln.Artifact.objects.count() == before + 1
    assert set(artifact._links_schema.values_list("slot", flat=True)) == {
        "samples.parquet",
        "expression.zarr::obs",
        "expression.zarr::var",
    }
    assert ln.Artifact.get(artifact.uid).schema == mixed


def test_folder_valid_change_after_preparation_rejected(folder_input):
    root, schema = folder_input
    artifact = ln.Artifact.from_folder(
        root, schema=schema, key="folder-tests/valid-change"
    )
    pd.DataFrame({"folder_sample_id": ["s3"]}).to_parquet(
        root / "samples.parquet", index=False
    )
    with pytest.raises(ln.errors.ValidationError, match="changed"):
        artifact.save()
    assert not ln.Artifact.filter(key="folder-tests/valid-change").exists()


@pytest.mark.parametrize("suffix", [".csv", ".tsv", ".parquet"])
def test_folder_table_formats_and_read_only(tmp_path, suffix):
    path = tmp_path / f"table{suffix}"
    frame = pd.DataFrame({"folder_format_value": [1.0, 2.0]})
    if suffix == ".parquet":
        frame.to_parquet(path, index=False)
    else:
        frame.to_csv(path, sep="\t" if suffix == ".tsv" else ",", index=False)
    original = path.read_bytes()
    child = ln.Schema(
        features=[ln.Feature(name="folder_format_value", dtype=float).save()],
        otype="DataFrame",
    ).save()
    schema = ln.Schema(otype="Folder", slots={path.name: child}).save()
    artifact = ln.Artifact.from_folder(
        tmp_path, schema=schema, key=f"folder-tests/format-{suffix[1:]}"
    ).save()
    assert path.read_bytes() == original
    assert artifact.schema == schema


def test_folder_existing_artifact_failure_preserves_schema(folder_input):
    root, schema = folder_input
    artifact = ln.Artifact(
        root, key="folder-tests/generic", skip_hash_lookup=True
    ).save()
    stored_path = artifact.path / "samples.parquet"
    stored_path.unlink()
    curator = ln.curators.FolderCurator(artifact, schema)
    with pytest.raises(ln.errors.ValidationError, match="samples.parquet"):
        curator.save_artifact()
    assert ln.Artifact.get(artifact.uid).schema is None
    assert artifact.schema is None


def test_folder_unsupported_otype(folder_input):
    root, schema = folder_input
    child = schema.slots["samples.parquet"]
    child.otype = "UnsupportedCustomObject"
    child.save()
    with pytest.raises(ln.errors.InvalidArgument, match="unsupported child otype"):
        ln.curators.FolderCurator(root, schema).validate()


def test_folder_spatialdata_child(tmp_path):
    sd = pytest.importorskip("spatialdata")
    feature = ln.Feature(name="folder_spatial_value", dtype=float).save()
    values = ln.Schema(features=[feature]).save()
    schema = ln.Schema(
        otype="SpatialData", slots={"attrs": values}, suffix=".zarr"
    ).save()
    sd.SpatialData(attrs={"folder_spatial_value": 1.0}).write(tmp_path / "spatial.zarr")
    folder = ln.Schema(
        otype="Folder", slots={"spatial.zarr": schema}, maximal_set=True
    ).save()
    artifact = ln.curators.FolderCurator(tmp_path, folder).save_artifact(
        key="folder-tests/spatial"
    )
    assert artifact._links_schema.get(slot="spatial.zarr::attrs")


def test_folder_tiledbsoma_child(tmp_path):
    soma = pytest.importorskip("tiledbsoma")
    io = pytest.importorskip("tiledbsoma.io")
    feature = ln.Feature(name="folder_soma_group", dtype=str).save()
    obs = ln.Schema(features=[feature]).save()
    schema = ln.Schema(otype="tiledbsoma", slots={"obs": obs}).save()
    data = ad.AnnData(
        obs=pd.DataFrame({"folder_soma_group": ["a", "b"]}, index=["s1", "s2"]),
        var=pd.DataFrame(index=["v1", "v2"]),
    )
    path = tmp_path / "experiment.tiledbsoma"
    io.from_anndata(str(path), data, measurement_name="RNA")
    folder = ln.Schema(
        otype="Folder", slots={path.name: schema}, maximal_set=True
    ).save()
    before = ln.Artifact.objects.count()
    ln.curators.FolderCurator(tmp_path, folder).validate()
    assert ln.Artifact.objects.count() == before
    artifact = ln.curators.FolderCurator(tmp_path, folder).save_artifact(
        key="folder-tests/soma"
    )
    assert artifact._links_schema.get(slot="experiment.tiledbsoma::obs")
    with soma.Experiment.open(str(path)):
        pass


def test_folder_wrong_loaded_object_type(tmp_path):
    child = ln.Schema(
        features=[ln.Feature(name="folder_wrong_type", dtype=str).save()],
        otype="DataFrame",
    ).save()
    ad.AnnData(np.ones((1, 1))).write_h5ad(tmp_path / "wrong.h5ad")
    schema = ln.Schema(otype="Folder", slots={"wrong.h5ad": child}).save()
    with pytest.raises(ln.errors.InvalidArgument, match="wrong.h5ad"):
        ln.curators.FolderCurator(tmp_path, schema).validate()


def test_folder_rejects_mutated_contract(folder_input):
    root, schema = folder_input
    curator = ln.curators.FolderCurator(root, schema)
    schema.minimal_set = False
    with pytest.raises(ln.errors.InvalidArgument, match="minimal_set"):
        curator.validate()


def test_folder_annotations_preserve_same_named_labels(tmp_path):
    label_a = ln.ULabel(name="folder-label-a").save()
    label_b = ln.ULabel(name="folder-label-b").save()
    feature = ln.Feature(name="folder_label", dtype=ln.ULabel).save()
    child = ln.Schema(features=[feature]).save()
    for name, label in [("one", label_a), ("two", label_b)]:
        pd.DataFrame({"folder_label": pd.Categorical([label.name])}).to_parquet(
            tmp_path / f"{name}.parquet", index=False
        )
    schema = ln.Schema(
        otype="Folder", slots={"one.parquet": child, "two.parquet": child}
    ).save()
    artifact = ln.curators.FolderCurator(tmp_path, schema).save_artifact(
        key="folder-tests/labels"
    )
    assert set(artifact.ulabels.values_list("name", flat=True)) == {
        label_a.name,
        label_b.name,
    }


def test_folder_root_symlink_rejected(folder_input, tmp_path):
    root, schema = folder_input
    link = tmp_path / "linked"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(ln.errors.InvalidArgument, match="symlink"):
        ln.curators.FolderCurator(link, schema).validate()


def test_folder_remote_path_rejected(folder_input):
    _, schema = folder_input
    with pytest.raises(ln.errors.InvalidArgument, match="local"):
        ln.curators.FolderCurator("s3://not-accessed/study", schema)


def test_folder_nested_paths_and_schema_roundtrip(folder_input):
    root, schema = folder_input
    nested = root / "tables"
    nested.mkdir()
    (root / "samples.parquet").rename(nested / "samples.parquet")
    child = schema.slots["samples.parquet"]
    schema = ln.Schema(
        otype="Folder", slots={"tables/samples.parquet": child}, maximal_set=True
    ).save()
    reloaded = ln.Schema.get(schema.uid)
    artifact = ln.Artifact.from_folder(
        root, schema=reloaded, key="folder-tests/nested"
    ).save()
    assert artifact._links_schema.get(slot="tables/samples.parquet")


def test_folder_failed_revalidation_resets_state(folder_input):
    root, schema = folder_input
    curator = ln.curators.FolderCurator(root, schema)
    curator.validate()
    assert curator._is_validated
    (root / "samples.parquet").unlink()
    with pytest.raises(ln.errors.ValidationError, match="samples.parquet"):
        curator.validate()
    assert not curator._is_validated


def test_folder_schema_hash_layout_semantics(folder_input):
    _, schema = folder_input
    child = schema.slots["samples.parquet"]
    renamed = ln.Schema(otype="Folder", slots={"other.parquet": child})
    permissive = ln.Schema(otype="Folder", slots=schema.slots, maximal_set=False)
    assert len({schema.hash, renamed.hash, permissive.hash}) == 3
    a = ln.Schema(otype="Folder", slots={"a.parquet": child, "b.parquet": child}).save()
    b = ln.Schema(otype="Folder", slots={"b.parquet": child, "a.parquet": child})
    assert a.hash == b.hash


def test_folder_revisions(folder_input):
    root, schema = folder_input
    first = ln.Artifact.from_folder(
        root, schema=schema, key="folder-tests/revisions"
    ).save()
    pd.DataFrame({"folder_sample_id": ["different"]}).to_parquet(
        root / "samples.parquet", index=False
    )
    second = ln.Artifact.from_folder(
        root, schema=schema, key=first.key, revises=first
    ).save()
    assert second.uid != first.uid
    assert second.uid[:16] == first.uid[:16]
    assert ln.Artifact.get(first.uid).is_latest is False
    assert second.schema == schema


def test_folder_schema_rejects_overlapping_objects(folder_input):
    _, schema = folder_input
    child = schema.slots["samples.parquet"]
    with pytest.raises(ln.errors.InvalidArgument, match="Overlapping"):
        ln.Schema(
            otype="Folder",
            slots={"data.zarr": child, "data.zarr/internal.csv": child},
        )


def test_folder_revalidates_generic_artifact(folder_input):
    root, schema = folder_input
    artifact = ln.Artifact(root, key="folder-tests/revalidate").save()
    before = ln.Artifact.objects.count()
    curated = ln.curators.FolderCurator(artifact, schema).save_artifact()
    assert curated.id == artifact.id
    assert ln.Artifact.objects.count() == before
    assert ln.Artifact.get(artifact.uid).schema == schema


def test_folder_documentation_example():
    import runpy
    from pathlib import Path

    script = Path(__file__).parents[2] / "docs/scripts/curate_folder.py"
    assert script.is_file()
    runpy.run_path(str(script), run_name="__main__")


def test_folder_anndata_registry_index_annotations(tmp_path):
    features = [
        ln.Feature(name="folder-var-one", dtype=float).save(),
        ln.Feature(name="folder-var-two", dtype=float).save(),
    ]
    var = ln.Schema(itype=ln.Feature.name, dtype=float).save()
    child = ln.Schema(otype="AnnData", slots={"var.T": var}).save()
    adata = ad.AnnData(
        np.ones((1, 2)),
        var=pd.DataFrame(index=[feature.name for feature in features]),
    )
    adata.write_h5ad(tmp_path / "registry.h5ad")
    schema = ln.Schema(otype="Folder", slots={"registry.h5ad": child}).save()
    artifact = ln.curators.FolderCurator(tmp_path, schema).save_artifact(
        key="folder-tests/registry"
    )
    inferred = artifact._links_schema.get(slot="registry.h5ad::var.T").schema
    assert inferred.itype.startswith("Feature")
    assert inferred.n_members == 2


def test_folder_recuration_with_new_schema_drops_old_slots(folder_input):
    root, schema = folder_input
    (root / "extra.parquet").write_bytes((root / "samples.parquet").read_bytes())
    child = schema.slots["samples.parquet"]
    both = ln.Schema(
        otype="Folder", slots={"samples.parquet": child, "extra.parquet": child}
    ).save()
    artifact = ln.curators.FolderCurator(root, both).save_artifact(
        key="folder-tests/drop-slots"
    )
    only_samples = ln.Schema(otype="Folder", slots={"samples.parquet": child}).save()
    ln.curators.FolderCurator(artifact, only_samples).save_artifact()
    reloaded = ln.Artifact.get(artifact.uid)
    assert reloaded.schema == only_samples
    assert set(reloaded._links_schema.values_list("slot", flat=True)) == {
        "samples.parquet"
    }


def test_folder_from_folder_on_registered_folder_applies_schema(folder_input):
    root, schema = folder_input
    existing = ln.Artifact(root, key="folder-tests/registered").save()
    artifact = ln.Artifact.from_folder(root, schema=schema)
    assert artifact.id == existing.id
    assert artifact.schema == schema
    artifact.save()
    assert ln.Artifact.get(existing.uid).schema == schema


def test_folder_rejects_non_folder_artifact_otype(folder_input):
    root, schema = folder_input
    artifact = ln.Artifact(root, key="folder-tests/otype").save()
    artifact.otype = "AnnData"
    with pytest.raises(ln.errors.InvalidArgument, match="otype"):
        ln.curators.FolderCurator(artifact, schema)


def test_folder_load_error_names_slot(folder_input):
    root, schema = folder_input
    (root / "samples.parquet").write_bytes(b"not a parquet file")
    with pytest.raises(Exception) as info:  # noqa: PT011
        ln.curators.FolderCurator(root, schema).validate()
    assert "folder slot: samples.parquet" in getattr(info.value, "__notes__", [])


def test_folder_save_after_validate_parses_once(folder_input, monkeypatch):
    from lamindb.curators import _folder

    root, schema = folder_input
    calls = []
    open_child = _folder._open_child
    monkeypatch.setattr(
        _folder,
        "_open_child",
        lambda path, otype: calls.append(path) or open_child(path, otype),
    )
    curator = ln.curators.FolderCurator(root, schema)
    curator.validate()
    curator.save_artifact(key="folder-tests/parse-once")
    assert len(calls) == 1


def test_folder_cache_check_against_cloud_listing(tmp_path):
    from lamindb.curators._folder import _check_cache_matches_cloud
    from upath import UPath

    cloud = UPath("memory:///folder-cache-check")
    (cloud / "sub").mkdir(parents=True, exist_ok=True)
    (cloud / "a.txt").write_bytes(b"abc")
    (cloud / "sub" / "b.txt").write_bytes(b"de")
    local = tmp_path / "cache"
    (local / "sub").mkdir(parents=True)
    (local / "a.txt").write_bytes(b"abc")
    (local / "sub" / "b.txt").write_bytes(b"de")
    try:
        _check_cache_matches_cloud(cloud, local)
        (local / "extra.txt").write_bytes(b"x")
        with pytest.raises(ln.errors.ValidationError, match="extra.txt"):
            _check_cache_matches_cloud(cloud, local)
        (local / "extra.txt").unlink()
        (local / "a.txt").write_bytes(b"abcd")
        with pytest.raises(ln.errors.ValidationError, match="a.txt"):
            _check_cache_matches_cloud(cloud, local)
    finally:
        cloud.fs.rm(str(cloud), recursive=True)
