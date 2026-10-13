"""Engine-independent inputs and outputs. Frames never cross this boundary."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

Checkpoint = Callable[[], object]


class EngineCapabilityError(ValueError):
    """The selected engine cannot preserve this operation's semantics."""


class EngineResourceLimitError(ValueError):
    """An attempt exceeded its resource budget before publication."""


@dataclass(frozen=True)
class SnapshotRef:
    path: Path


@dataclass(frozen=True)
class TransformationPlan:
    operation: str
    parameters: Mapping[str, Any]
    operation_version: str = "1.0.0"

    def validate_schema(self, schema: Any, *, engine_name: str) -> list[tuple[str, str]]:
        """Check native projection semantics from Arrow metadata before execution."""
        projection = self.projection(schema.names)
        if engine_name == "duckdb":
            import pyarrow as pa

            # DuckDB resolves quoted identifiers without case sensitivity.
            for names in (schema.names, [target for _, target in projection]):
                folded = [name.lower() for name in names]
                if len(set(folded)) != len(folded):
                    raise EngineCapabilityError("DuckDB requires column names distinct ignoring case.")
            for source, _ in projection:
                dtype = schema.field(source).type
                # DuckDB's zoned timestamp stores microseconds. Even aligned
                # values lose their declared nanosecond schema on roundtrip.
                if pa.types.is_timestamp(dtype) and dtype.tz and dtype.unit == "ns":
                    raise EngineCapabilityError(
                        f"The duckdb engine cannot preserve the type of column {source!r}. "
                        "Choose pandas explicitly."
                    )
        return projection

    def projection(self, columns: list[str]) -> list[tuple[str, str]]:
        """Compile a projection without interpreting names as SQL or expressions."""
        if len(columns) != len(set(columns)):
            raise EngineCapabilityError("Input contains duplicate column names.")
        from app.services.operations import operation_definition

        definition = operation_definition(self.operation, self.operation_version)
        if definition.projection_handler is None:
            raise EngineCapabilityError(
                "The native engines currently support drop_columns and rename_columns only. "
                "Choose pandas explicitly for other operations."
            )
        result = definition.projection_handler(columns, dict(self.parameters))
        if not result:
            raise ValueError("A dataset must keep at least one column.")
        return result


@dataclass(frozen=True)
class ResourcePolicy:
    max_output_bytes: int
    quota_remaining_bytes: int
    threads: int = 2
    memory_limit_bytes: int = 512 * 1024 * 1024
    max_spill_bytes: int = 1024 * 1024 * 1024
    timeout_seconds: float = 240
    checkpoint_seconds: float = 1
    max_columns: int = 1000
    max_column_name_chars: int = 256
    max_rows: int = 1_000_000

    def __post_init__(self) -> None:
        if min(self.max_output_bytes, self.quota_remaining_bytes, self.max_spill_bytes) < 0:
            raise ValueError("Resource budgets cannot be negative.")
        if min(
            self.threads, self.memory_limit_bytes, self.timeout_seconds, self.checkpoint_seconds,
            self.max_columns, self.max_column_name_chars, self.max_rows,
        ) <= 0:
            raise ValueError("Execution limits must be positive.")

    @classmethod
    def configured(cls, *, quota_remaining_bytes: int | None = None) -> "ResourcePolicy":
        from app.core.config import settings

        return cls(
            max_output_bytes=settings.MAX_DATASET_EXPANDED_SIZE_MB * 1024 * 1024,
            quota_remaining_bytes=(quota_remaining_bytes if quota_remaining_bytes is not None
                                   else settings.USER_STORAGE_QUOTA_MB * 1024 * 1024),
            threads=settings.TRANSFORMATION_ENGINE_THREADS,
            memory_limit_bytes=settings.TRANSFORMATION_ENGINE_MEMORY_MB * 1024 * 1024,
            max_spill_bytes=settings.TRANSFORMATION_ENGINE_SPILL_MB * 1024 * 1024,
            timeout_seconds=settings.TRANSFORMATION_ENGINE_TIMEOUT_SECONDS,
            max_columns=settings.MAX_DATASET_COLUMNS,
            max_column_name_chars=settings.MAX_DATASET_COLUMN_NAME_CHARS,
            max_rows=settings.MAX_DATASET_ROWS,
        )

    @property
    def output_limit_bytes(self) -> int:
        return min(self.max_output_bytes, self.quota_remaining_bytes)


@dataclass(frozen=True)
class MaterializedArtifact:
    path: Path
    before_rows: int
    before_columns: int
    rows: int
    columns: int
    size_bytes: int
    sha256: str
    engine: str
    semantics: str


class TransformationEngineAdapter(Protocol):
    name: str

    def materialize(
        self,
        snapshot: SnapshotRef,
        plan: TransformationPlan,
        output_path: Path,
        policy: ResourcePolicy,
        checkpoint: Checkpoint | None = None,
    ) -> MaterializedArtifact: ...
