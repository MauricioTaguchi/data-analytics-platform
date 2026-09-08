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

    def projection(self, columns: list[str]) -> list[tuple[str, str]]:
        """Compile a projection without interpreting names as SQL or expressions."""
        if len(columns) != len(set(columns)):
            raise EngineCapabilityError("Input contains duplicate column names.")
        if self.operation == "drop_columns":
            requested = self.parameters.get("columns", [])
            if not isinstance(requested, list) or any(not isinstance(name, str) for name in requested):
                raise ValueError("columns must be a list of column names.")
            unknown = set(requested) - set(columns)
            if unknown:
                raise ValueError(f"Unknown columns: {', '.join(sorted(unknown))}")
            dropped = set(requested)
            result = [(name, name) for name in columns if name not in dropped]
        elif self.operation == "rename_columns":
            mapping = self.parameters.get("mapping", {})
            if not isinstance(mapping, dict) or any(
                not isinstance(source, str) or not isinstance(target, str) or "\x00" in target
                for source, target in mapping.items()
            ):
                raise ValueError("mapping must map column names to valid string names.")
            unknown = set(mapping) - set(columns)
            if unknown:
                raise ValueError(f"Unknown columns: {', '.join(sorted(unknown))}")
            result = [(name, mapping.get(name, name)) for name in columns]
            renamed = [target for _, target in result]
            if len(renamed) != len(set(renamed)):
                raise ValueError("Renaming would create duplicate column names.")
        else:
            raise EngineCapabilityError(
                "The native engines currently support drop_columns and rename_columns only. "
                "Choose pandas explicitly for other operations."
            )
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
