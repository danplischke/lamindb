"""Curators.

High-level curators
-------------------

.. autoclass:: DataFrameCurator
.. autoclass:: FolderCurator
.. autoclass:: AnnDataCurator
.. autoclass:: MuDataCurator
.. autoclass:: SpatialDataCurator
.. autoclass:: TiledbsomaExperimentCurator

Low-level module
----------------

.. autosummary::
   :toctree: .

   core

"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._folder import FolderCurator
    from .core import (
        AnnDataCurator,
        DataFrameCurator,
        MuDataCurator,
        SpatialDataCurator,
        TiledbsomaExperimentCurator,
    )

__all__ = [
    "AnnDataCurator",
    "DataFrameCurator",
    "FolderCurator",
    "MuDataCurator",
    "SpatialDataCurator",
    "TiledbsomaExperimentCurator",
]

_CURATOR_NAMES = frozenset(__all__)


def __getattr__(name: str):
    """Lazy-import curators from core to avoid loading pandas/pandera at import."""
    if name in _CURATOR_NAMES:
        if name == "FolderCurator":
            from ._folder import FolderCurator

            globals()[name] = FolderCurator
            return FolderCurator
        from . import core

        attr = getattr(core, name)
        globals()[name] = attr
        return attr
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
