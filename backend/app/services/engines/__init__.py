"""Pluggable transformation engines with no native imports in the worker parent."""

from .contracts import (
    EngineCapabilityError,
    EngineResourceLimitError,
    MaterializedArtifact,
    ResourcePolicy,
    SnapshotRef,
    TransformationEngineAdapter,
    TransformationPlan,
)
from .native import DuckDBEngineAdapter, PolarsEngineAdapter
from .pandas_adapter import PandasEngineAdapter


def select_engine(name: str, *, pandas: PandasEngineAdapter) -> TransformationEngineAdapter:
    if name == "pandas":
        return pandas
    if name == "polars":
        return PolarsEngineAdapter()
    if name == "duckdb":
        return DuckDBEngineAdapter()
    raise EngineCapabilityError(f"Unknown transformation engine: {name}.")


__all__ = [
    "DuckDBEngineAdapter",
    "EngineCapabilityError",
    "EngineResourceLimitError",
    "MaterializedArtifact",
    "PandasEngineAdapter",
    "PolarsEngineAdapter",
    "ResourcePolicy",
    "SnapshotRef",
    "TransformationEngineAdapter",
    "TransformationPlan",
    "select_engine",
]
