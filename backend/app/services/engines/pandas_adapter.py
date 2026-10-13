from collections.abc import Callable
from pathlib import Path
from typing import Any

from .artifact import artifact_digest, validate_paths
from .contracts import Checkpoint, MaterializedArtifact, ResourcePolicy, SnapshotRef, TransformationPlan


class PandasEngineAdapter:
    """Supervised by default; explicit callbacks provide an injectable local adapter.

    Production callers construct this without callbacks. The child and targeted
    tests inject the pure reader/handler/writer dependencies explicitly.
    """

    name = "pandas"

    def __init__(
        self,
        *,
        read: Callable[..., Any] | None = None,
        transform: Callable[..., Any] | None = None,
        validate: Callable[..., Any] | None = None,
        write: Callable[..., Any] | None = None,
    ) -> None:
        if any(item is not None for item in (read, transform, validate, write)) and not all(
            item is not None for item in (read, transform, validate, write)
        ):
            raise ValueError("An injected Pandas adapter requires all four dependencies.")
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
        if self._read is None:
            from .runner import run_native

            return run_native(self.name, snapshot, plan, output_path, policy, checkpoint)
        assert self._transform is not None and self._validate is not None and self._write is not None
        validate_paths(snapshot.path, output_path)
        if checkpoint:
            checkpoint()
        before = self._read(snapshot.path)
        before_rows, before_columns = before.shape
        if checkpoint:
            checkpoint()
        after = self._transform(before, plan.operation, dict(plan.parameters), version=plan.operation_version)
        # The operation owns its result; release the source frame before Arrow
        # allocates serialization buffers for the immutable output snapshot.
        del before
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
            semantics=f"pandas:{plan.operation}@{plan.operation_version}",
        )
