from pathlib import Path
from tempfile import TemporaryDirectory

import anndata as ad
import lamindb as ln
import numpy as np
import pandas as pd

sample_schema = ln.Schema(
    features=[ln.Feature(name="folder_example_sample_id", dtype=str).save()],
    otype="DataFrame",
).save()
protein_schema = ln.Schema(
    features=[ln.Feature(name="folder_example_protein_id", dtype=str).save()],
    otype="DataFrame",
).save()
expression_schema = ln.Schema(
    otype="AnnData",
    slots={"obs": sample_schema, "var": protein_schema},
    suffix=".zarr",
).save()
folder_schema = ln.Schema(
    name="Proteomics folder example",
    otype="Folder",
    slots={
        "samples.parquet": sample_schema,
        "expression.zarr": expression_schema,
    },
    maximal_set=True,
).save()

with TemporaryDirectory() as directory:
    root = Path(directory)
    samples = pd.DataFrame(
        {"folder_example_sample_id": ["s1", "s2"]}, index=["s1", "s2"]
    )
    samples.to_parquet(root / "samples.parquet")
    adata = ad.AnnData(
        X=np.array([[1.0, 2.0], [3.0, 4.0]]),
        obs=samples,
        var=pd.DataFrame(
            {"folder_example_protein_id": ["P16860", "P19429"]},
            index=["P16860", "P19429"],
        ),
    )
    adata.write_zarr(root / "expression.zarr")

    curator = ln.curators.FolderCurator(root, folder_schema)
    curator.validate()
    artifact = curator.save_artifact(key="examples/proteomics-folder")

    assert ln.Artifact.get(artifact.uid).schema == folder_schema
    assert set(artifact.features.slots) == {
        "samples.parquet",
        "expression.zarr::obs",
        "expression.zarr::var",
    }
