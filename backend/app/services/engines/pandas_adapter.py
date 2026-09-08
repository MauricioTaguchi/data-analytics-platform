from collections.abc import Callable
from pathlib import Path
from typing import Any

from .artifact import artifact_digest, validate_paths
from .contracts import Checkpoint, MaterializedArtifact, ResourcePolicy, SnapshotRef, TransformationPlan


class PandasEngineAdapter:
    """Bridge to existing format readers and guarded writers without a service import cycle."""

    name = "pandas"

    def __init__(
        self,
        *,
        read: Callable[..., Any],
        transform: Callable[..., Any],
        validate: Callable[..., Any],
        write: Callable[..., Any],
    ) -> None:
        self._read = read
        self._transform = transform
        self._validate = validate
        self._write = write

    def materialize(
        self,
        snapshot: SnapshotRef,
        plan: TransformationPlan,
        output_path: Path,
        policy: ResourcePolicy,
        checkpoint: Checkpoint | None = None,
    ) -> MaterializedArtifact:
        validate_paths(snapshot.path, output_path)
        if checkpoint:
            checkpoint()
        before = self._read(snapshot.path)
        before_rows, before_columns = before.shape
        if checkpoint:
            checkpoint()
        after = self._transform(before, plan.operation, dict(plan.parameters))
        self._validate(after)
        if checkpoint:
            checkpoint()
        self._write(
            after,
            output_path,
            expanded_limit_bytes=policy.max_output_bytes,
            quota_remaining_bytes=policy.quota_remaining_bytes,
        )
        size, digest = artifact_digest(output_path, policy)
        if checkpoint:
            checkpoint()
        return MaterializedArtifact(
            path=output_path,
            before_rows=int(before_rows),
            before_columns=int(before_columns),
            rows=int(after.shape[0]),
            columns=int(after.shape[1]),
            size_bytes=size,
            sha256=digest,
            engine=self.name,
            semantics="pandas-v1",
        )
