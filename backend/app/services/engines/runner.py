"""Supervise dataframe work in fresh processes, including Pandas.

The watchdog samples RSS and spill usage; container/cgroup limits are the hard
aggregate boundary. Cancellation and lease loss kill and join the child before
returning, so a cancelled attempt cannot continue consuming worker resources.
"""

import errno
import json
import os
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from .artifact import validate_paths
from .contracts import (
    Checkpoint,
    EngineCapabilityError,
    EngineResourceLimitError,
    MaterializedArtifact,
    ResourcePolicy,
    SnapshotRef,
    TransformationPlan,
)

MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_RESULT_BYTES = 2 * 1024 * 1024
MEMORY_LIMIT_EXIT_CODE = 73


def _directory_bytes(directory: Path) -> int:
    total = 0
    for path in directory.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except FileNotFoundError:
            pass
    return total


def _resident_bytes(pid: int) -> int:
    """Linux-only sampled RSS; /proc absence is not a hard memory guarantee."""
    try:
        pages = int(Path(f"/proc/{pid}/statm").read_text().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError, AttributeError):
        return 0


def spill_directory(output_path: Path) -> Path:
    return output_path.with_name(f".{output_path.name}.spill")


@contextmanager
def _managed_spill_directory(output_path: Path):
    directory = spill_directory(output_path)
    directory.mkdir(mode=0o700, exist_ok=False)
    try:
        yield directory
    finally:
        try:
            shutil.rmtree(directory)
        except OSError:
            # The durable artifact reservation can reclaim an abandoned spill.
            pass


def _execution_settings() -> dict:
    from app.core.config import settings

    # Propagate effective parsing limits, including explicit dependency overrides.
    # Never serialize credentials or the entire application configuration.
    names = (
        "MAX_DATASET_ROWS", "MAX_DATASET_COLUMNS", "MAX_DATASET_COLUMN_NAME_CHARS",
        "MAX_DATASET_EXPANDED_SIZE_MB", "MAX_DATASET_EXPANSION_RATIO", "MAX_XLSX_ARCHIVE_ENTRIES",
        "MAX_PROFILE_CORRELATION_COLUMNS", "TRANSFORMATION_PREVIEW_MAX_COLUMNS",
        "TRANSFORMATION_PREVIEW_MAX_CELL_CHARS",
        "MIN_FREE_DISK_SPACE_MB",
    )
    return {name: getattr(settings, name) for name in names if hasattr(settings, name)}


def _run_job(
    payload: dict, policy: ResourcePolicy, directory: Path, checkpoint: Checkpoint | None,
    *, output_path: Path | None = None,
) -> dict:
    if checkpoint:
        checkpoint()
    payload.update(policy=asdict(policy), settings=_execution_settings(), spill_path=str(directory))
    encoded = (json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n").encode()
    if len(encoded) > MAX_REQUEST_BYTES:
        raise EngineResourceLimitError("Execution request exceeds its metadata budget.")
    environment = os.environ.copy()
    for name in ("POLARS_MAX_THREADS", "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        environment[name] = str(policy.threads)
    started = time.monotonic()
    process = subprocess.Popen(
        [sys.executable, "-m", "app.services.engines.runner"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=environment,
        cwd=Path(__file__).resolve().parents[3],
    )
    chunks: list[bytes] = []
    overflow = threading.Event()

    def drain_result() -> None:
        assert process.stdout is not None
        total = 0
        while data := process.stdout.read(4096):
            total += len(data)
            if total > MAX_RESULT_BYTES:
                overflow.set()
            elif not overflow.is_set():
                chunks.append(data)

    reader = threading.Thread(target=drain_result, daemon=True)
    reader.start()
    try:
        assert process.stdin is not None
        process.stdin.write(encoded)
        process.stdin.flush()
        while True:
            remaining = policy.timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise EngineResourceLimitError("Transformation execution deadline exceeded.")
            try:
                process.wait(timeout=min(policy.checkpoint_seconds, remaining))
                break
            except subprocess.TimeoutExpired:
                if checkpoint:
                    checkpoint()
                if time.monotonic() - started >= policy.timeout_seconds:
                    raise EngineResourceLimitError("Transformation execution deadline exceeded.") from None
                if _resident_bytes(process.pid) > policy.memory_limit_bytes:
                    raise EngineResourceLimitError("Transformation process exceeded its memory budget.") from None
                if _directory_bytes(directory) > policy.max_spill_bytes:
                    raise EngineResourceLimitError("Transformation spill budget exceeded.") from None
                if output_path and output_path.exists() and output_path.stat().st_size > policy.output_limit_bytes:
                    raise EngineResourceLimitError("Transformation output budget exceeded.") from None
                if overflow.is_set():
                    raise EngineResourceLimitError("Execution result exceeds its metadata budget.") from None
        reader.join()
        if overflow.is_set():
            raise EngineResourceLimitError("Execution result exceeds its metadata budget.")
        response_bytes = b"".join(chunks)
        if not response_bytes:
            if process.returncode == MEMORY_LIMIT_EXIT_CODE:
                raise EngineResourceLimitError("Transformation process exceeded its memory budget.")
            raise EngineResourceLimitError(
                f"Transformation process exited without a result (exit {process.returncode})."
            )
        response = json.loads(response_bytes)
        if "error" in response:
            from app.services.dataset_service import DatasetExpansionLimitError, UserStorageQuotaError

            exception_type = {
                "capability": EngineCapabilityError, "resource": EngineResourceLimitError,
                "expansion": DatasetExpansionLimitError, "quota": UserStorageQuotaError,
            }.get(response.get("kind"), ValueError)
            raise exception_type(response["error"])
        if process.returncode != 0:
            raise ValueError("Transformation process failed.")
        if checkpoint:
            checkpoint()
        return response
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        reader.join()
        if process.stdin:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.stdout:
            process.stdout.close()


def run_native(
    engine: str, snapshot: SnapshotRef, plan: TransformationPlan, output_path: Path,
    policy: ResourcePolicy, checkpoint: Checkpoint | None = None,
) -> MaterializedArtifact:
    """Run an explicitly selected engine. There is no eager native fallback."""
    validate_paths(snapshot.path, output_path)
    if engine != "pandas":
        if snapshot.path.suffix.lower() != ".parquet" or output_path.suffix.lower() != ".parquet":
            raise EngineCapabilityError("Native engines require Parquet input and output.")
        if plan.operation not in {"drop_columns", "rename_columns"}:
            raise EngineCapabilityError("This operation requires the pandas engine.")
    with _managed_spill_directory(output_path) as directory:
        response = _run_job({
            "kind": "transform", "engine": engine, "input_path": str(snapshot.path.resolve()),
            "output_path": str(output_path.resolve()), "operation": plan.operation,
            "operation_version": plan.operation_version, "parameters": dict(plan.parameters),
        }, policy, directory, checkpoint, output_path=output_path)
    response["path"] = output_path
    return MaterializedArtifact(**response)


def _run_metadata(kind: str, snapshot: SnapshotRef, policy: ResourcePolicy, checkpoint, **parameters) -> dict:
    with tempfile.TemporaryDirectory(prefix="dataflow-execution-") as directory:
        return _run_job({
            "kind": kind, "input_path": str(snapshot.path.resolve()), **parameters,
        }, policy, Path(directory), checkpoint)


def run_quality(snapshot: SnapshotRef, rules: list[dict], policy: ResourcePolicy, checkpoint=None) -> dict:
    return _run_metadata("quality", snapshot, policy, checkpoint, rules=rules)


def run_profile(snapshot: SnapshotRef, policy: ResourcePolicy, checkpoint=None) -> dict:
    return _run_metadata("profile", snapshot, policy, checkpoint)


def run_inspection(snapshot: SnapshotRef, policy: ResourcePolicy, checkpoint=None) -> dict:
    return _run_metadata("inspection", snapshot, policy, checkpoint)


def run_preview(
    snapshot: SnapshotRef, plan: TransformationPlan, policy: ResourcePolicy, checkpoint=None,
    *, engine_name: str = "pandas", output_path: Path,
) -> dict:
    validate_paths(snapshot.path, output_path)
    with _managed_spill_directory(output_path) as directory:
        return _run_job({
            "kind": "preview", "input_path": str(snapshot.path.resolve()),
            "output_path": str(output_path.resolve()), "engine": engine_name,
            "operation": plan.operation, "operation_version": plan.operation_version,
            "parameters": dict(plan.parameters),
        }, policy, directory, checkpoint, output_path=output_path)


def _materialize_in_child(engine, snapshot, plan, output_path, spill_path, policy):
    from app.services.operations import validate_operation_capability

    validate_operation_capability(
        plan.operation, dict(plan.parameters), version=plan.operation_version,
        engine_name=engine, input_path=snapshot.path,
    )
    if engine == "pandas":
        from app.services.dataset_service import DatasetService
        from .pandas_adapter import PandasEngineAdapter

        adapter = PandasEngineAdapter(
            read=DatasetService.read_dataframe, transform=DatasetService.apply_operation,
            validate=DatasetService.validate_dataframe_structure, write=DatasetService.write_dataframe_limited,
        )
        return adapter.materialize(snapshot, plan, output_path, policy)
    from .native import materialize_in_child

    return materialize_in_child(engine, snapshot, plan, output_path, spill_path, policy)


def _exit_when_parent_closes_input() -> None:
    if not sys.stdin.buffer.read(1):
        os._exit(1)


def _child_dispatch(payload: dict, policy: ResourcePolicy) -> dict:
    from types import SimpleNamespace
    from app.core.config import settings

    for name, value in payload["settings"].items():
        setattr(settings, name, value)
    settings.MAX_DATASET_ROWS = policy.max_rows
    settings.MAX_DATASET_COLUMNS = policy.max_columns
    settings.MAX_DATASET_COLUMN_NAME_CHARS = policy.max_column_name_chars
    snapshot = SnapshotRef(Path(payload["input_path"]))
    kind = payload["kind"]
    if kind == "quality":
        from app.services.quality_service import evaluate_quality

        return evaluate_quality(snapshot.path, payload["rules"])
    if kind in {"profile", "inspection", "preview"}:
        from app.services.dataset_service import DatasetService

        source = SimpleNamespace(stored_path=str(snapshot.path), status="ready")
        if kind == "profile":
            return DatasetService.build_profile(source)
        if kind == "inspection":
            return DatasetService.inspect_staged_dataset(source)
        class ChildAdapter:
            def materialize(self, snapshot, plan, output_path, policy, checkpoint=None):
                return _materialize_in_child(
                    payload["engine"], snapshot, plan, output_path, Path(payload["spill_path"]), policy,
                )

        return DatasetService.preview_transformation(
            source, payload["operation"], payload["parameters"], engine_name=payload["engine"],
            output_path=Path(payload["output_path"]), quota_remaining_bytes=policy.quota_remaining_bytes,
            operation_version=payload["operation_version"], _child_adapter=ChildAdapter(),
        )
    plan = TransformationPlan(payload["operation"], payload["parameters"], payload["operation_version"])
    artifact = _materialize_in_child(
        payload["engine"], snapshot, plan, Path(payload["output_path"]), Path(payload["spill_path"]), policy,
    )
    result = asdict(artifact)
    result["path"] = str(artifact.path)
    return result


def _peak_resident_bytes() -> int:
    try:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(peak if sys.platform == "darwin" else peak * 1024)
    except (ImportError, AttributeError, OSError):
        return 0


def _watch_child_memory(policy: ResourcePolicy) -> None:
    while True:
        if _peak_resident_bytes() > policy.memory_limit_bytes:
            os._exit(MEMORY_LIMIT_EXIT_CODE)
        time.sleep(min(policy.checkpoint_seconds, 0.1))


def _child_main() -> None:
    try:
        encoded = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1)
        if len(encoded) > MAX_REQUEST_BYTES:
            raise EngineResourceLimitError("Execution request exceeds its metadata budget.")
        payload = json.loads(encoded)
        threading.Thread(target=_exit_when_parent_closes_input, daemon=True).start()
        policy = ResourcePolicy(**payload["policy"])
        threading.Thread(target=_watch_child_memory, args=(policy,), daemon=True).start()
        if os.name == "posix":
            import resource

            resource.setrlimit(resource.RLIMIT_FSIZE, (policy.output_limit_bytes, policy.output_limit_bytes))
        result = _child_dispatch(payload, policy)
        if _peak_resident_bytes() > policy.memory_limit_bytes:
            raise EngineResourceLimitError("Transformation process exceeded its memory budget.")
    except Exception as exc:
        kind = "failure"
        if isinstance(exc, EngineCapabilityError):
            kind = "capability"
        elif isinstance(exc, (EngineResourceLimitError, MemoryError)) or (
            isinstance(exc, OSError) and exc.errno in {errno.EFBIG, errno.ENOSPC}
        ):
            kind = "resource"
        elif type(exc).__name__ == "DatasetExpansionLimitError":
            kind = "expansion"
        elif type(exc).__name__ == "UserStorageQuotaError":
            kind = "quota"
        result = {"error": str(exc)[:1000], "kind": kind}
    encoded_result = json.dumps(result, allow_nan=False)
    if len(encoded_result.encode()) > MAX_RESULT_BYTES:
        encoded_result = json.dumps({"error": "Execution result exceeds its metadata budget.", "kind": "resource"})
    print(encoded_result, flush=True)
    os._exit(0 if "error" not in result else 1)


if __name__ == "__main__":
    _child_main()
