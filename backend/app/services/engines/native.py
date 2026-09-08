"""Trusted projection compilers. Native dependencies are imported only in an exec child."""

from pathlib import Path

from .artifact import artifact_digest
from .contracts import (
    Checkpoint,
    EngineCapabilityError,
    MaterializedArtifact,
    ResourcePolicy,
    SnapshotRef,
    TransformationPlan,
)


class PolarsEngineAdapter:
    name = "polars"

    def materialize(
        self,
        snapshot: SnapshotRef,
        plan: TransformationPlan,
        output_path: Path,
        policy: ResourcePolicy,
        checkpoint: Checkpoint | None = None,
    ) -> MaterializedArtifact:
        from .runner import run_native

        return run_native(self.name, snapshot, plan, output_path, policy, checkpoint)


class DuckDBEngineAdapter(PolarsEngineAdapter):
    name = "duckdb"


def _quote_identifier(value: str) -> str:
    if "\x00" in value:
        raise ValueError("Column names cannot contain null bytes.")
    return '"' + value.replace('"', '""') + '"'


def materialize_in_child(
    engine: str,
    snapshot: SnapshotRef,
    plan: TransformationPlan,
    output_path: Path,
    spill_path: Path,
    policy: ResourcePolicy,
) -> MaterializedArtifact:
    import pyarrow as pa
    import pyarrow.parquet as pq

    with pq.ParquetFile(snapshot.path) as source:
        source_schema = source.schema_arrow
        columns = source_schema.names
        before_rows = source.metadata.num_rows
    projection = plan.projection(columns)
    if before_rows > policy.max_rows:
        raise ValueError(f"Datasets are limited to {policy.max_rows:,} rows.")
    if len(projection) > policy.max_columns:
        raise ValueError(f"Datasets are limited to {policy.max_columns:,} columns.")
    if any(len(target) > policy.max_column_name_chars for _, target in projection):
        raise ValueError(f"Dataset column names are limited to {policy.max_column_name_chars:,} characters.")
    try:
        if engine == "polars":
            import polars as pl

            frame = pl.scan_parquet(snapshot.path, glob=False)
            # A name like '*' or '^.*$' has expression semantics in pl.col().
            # Positional selectors keep every source name strictly literal.
            positions = {name: index for index, name in enumerate(columns)}
            frame.select([pl.nth(positions[source]).alias(target) for source, target in projection]).sink_parquet(
                output_path,
                compression="zstd",
                row_group_size=131_072,
                maintain_order=True,
                engine="streaming",
            )
        elif engine == "duckdb":
            import duckdb

            if any(character in str(snapshot.path) for character in "*?[]"):
                raise EngineCapabilityError("DuckDB snapshots require a literal path without glob characters.")
            # DuckDB resolves quoted identifiers without case sensitivity. Reject
            # ambiguous schemas instead of silently renaming or selecting a neighbor.
            for names in (columns, [target for _, target in projection]):
                folded = [name.lower() for name in names]
                if len(set(folded)) != len(folded):
                    raise EngineCapabilityError("DuckDB requires column names distinct ignoring case.")
            with duckdb.connect(
                database=":memory:",
                config={
                    "threads": policy.threads,
                    "memory_limit": f"{policy.memory_limit_bytes}B",
                    "temp_directory": str(spill_path),
                    "max_temp_directory_size": f"{policy.max_spill_bytes}B",
                    "preserve_insertion_order": True,
                    "autoinstall_known_extensions": False,
                    "autoload_known_extensions": False,
                    "allow_community_extensions": False,
                },
            ) as connection:
                # Paths come from immutable storage references, never SQL input.
                relation = connection.read_parquet(str(snapshot.path))
                expression = ", ".join(
                    f"{_quote_identifier(source)} AS {_quote_identifier(target)}"
                    for source, target in projection
                )
                relation.project(expression).to_parquet(
                    str(output_path), compression="zstd", row_group_size=131_072
                )
        else:
            raise EngineCapabilityError(f"Unknown transformation engine: {engine}.")
    except ModuleNotFoundError as exc:
        raise EngineCapabilityError(
            f"The {engine} engine is not installed. Install requirements-engines.txt."
        ) from exc
    size, digest = artifact_digest(output_path, policy)
    with pq.ParquetFile(output_path) as result:
        rows = result.metadata.num_rows
        result_schema = result.schema_arrow
        output_columns = len(result_schema.names)
    if rows != before_rows or result_schema.names != [target for _, target in projection]:
        raise EngineCapabilityError("The engine changed the projection's row count or column names.")
    for source, target in projection:
        expected_type = source_schema.field(source).type
        actual_type = result_schema.field(target).type
        # Arrow's 32/64-bit offsets affect representation, not scalar semantics.
        same_strings = (
            pa.types.is_string(expected_type) or pa.types.is_large_string(expected_type)
        ) and (pa.types.is_string(actual_type) or pa.types.is_large_string(actual_type))
        same_binary = (
            pa.types.is_binary(expected_type) or pa.types.is_large_binary(expected_type)
        ) and (pa.types.is_binary(actual_type) or pa.types.is_large_binary(actual_type))
        if expected_type != actual_type and not same_strings and not same_binary:
            raise EngineCapabilityError(
                f"The {engine} engine cannot preserve the type of column {source!r}. Choose pandas explicitly."
            )
    return MaterializedArtifact(
        path=output_path,
        before_rows=before_rows,
        before_columns=len(columns),
        rows=rows,
        columns=output_columns,
        size_bytes=size,
        sha256=digest,
        engine=engine,
        semantics="projection-v1",
    )
