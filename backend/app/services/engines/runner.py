"""Supervise native processing without sharing Celery's forked native thread pools.

The watchdog samples RSS and spill usage; container/cgroup limits remain necessary
for a hard aggregate memory/disk boundary. POSIX also enforces a per-file size cap.
"""

import json
import os
import errno
from dataclasses import asdict
from pathlib import Path
import subprocess
import sys
from contextlib import contextmanager
import shutil
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


def _directory_bytes(directory: Path) -> int:
    total = 0
    for path in directory.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except FileNotFoundError:
            pass  # A spill file can disappear between enumeration and stat.
    return total


def _resident_bytes(pid: int) -> int:
    """Linux-only sampled RSS; absence of /proc does not imply a hard memory cap."""
    try:
        pages = int(Path(f"/proc/{pid}/statm").read_text().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError, AttributeError):
        return 0


def spill_directory(output_path: Path) -> Path:
    """Deterministic directory also reclaimed by the artifact reservation collector."""
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
            # Cleanup is recoverable through the durable artifact reservation.
            # It must not replace a lost-lease error or invalidate a good result.
            pass


def run_native(
    engine: str,
    snapshot: SnapshotRef,
    plan: TransformationPlan,
    output_path: Path,
    policy: ResourcePolicy,
    checkpoint: Checkpoint | None = None,
) -> MaterializedArtifact:
    validate_paths(snapshot.path, output_path)
    if snapshot.path.suffix.lower() != ".parquet" or output_path.suffix.lower() != ".parquet":
        raise EngineCapabilityError("Native engines require Parquet input and output.")
    if plan.operation not in {"drop_columns", "rename_columns"}:
        raise EngineCapabilityError("This operation requires the pandas engine.")
    if checkpoint:
        checkpoint()
    environment = os.environ.copy()
    # These values must be set before importing either native runtime.
    environment["POLARS_MAX_THREADS"] = str(policy.threads)
    environment["OMP_NUM_THREADS"] = str(policy.threads)
    environment["OPENBLAS_NUM_THREADS"] = str(policy.threads)
    with _managed_spill_directory(output_path) as directory:
        payload = {
            "engine": engine,
            "input_path": str(snapshot.path.resolve()),
            "output_path": str(output_path.resolve()),
            "spill_path": str(directory),
            "operation": plan.operation,
            "parameters": dict(plan.parameters),
            "policy": asdict(policy),
        }
        started = time.monotonic()
        process = subprocess.Popen(
            [sys.executable, "-m", "app.services.engines.runner"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=environment,
        )
        try:
            assert process.stdin is not None
            process.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())
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
                    if time.monotonic() - started > policy.timeout_seconds:
                        raise EngineResourceLimitError("Transformation execution deadline exceeded.") from None
                    if _resident_bytes(process.pid) > policy.memory_limit_bytes:
                        raise EngineResourceLimitError("Transformation process exceeded its memory budget.") from None
                    if _directory_bytes(Path(directory)) > policy.max_spill_bytes:
                        raise EngineResourceLimitError("Transformation spill budget exceeded.") from None
                    if output_path.exists() and output_path.stat().st_size > policy.output_limit_bytes:
                        raise EngineResourceLimitError("Transformation output budget exceeded.") from None
            # The child emits exactly one bounded metadata document. Keeping stdin
            # open until exit also provides a parent-lifetime channel to the child.
            assert process.stdout is not None
            response_bytes = process.stdout.read(16_384)
            if not response_bytes:
                raise EngineResourceLimitError(
                    f"Transformation process exited without a result (exit {process.returncode})."
                )
            response = json.loads(response_bytes)
            if "error" in response:
                exception_type = {
                    "capability": EngineCapabilityError,
                    "resource": EngineResourceLimitError,
                }.get(response.get("kind"), ValueError)
                raise exception_type(response["error"])
            if process.returncode != 0:
                raise ValueError("Transformation process failed.")
            if checkpoint:
                checkpoint()
            response["path"] = output_path
            return MaterializedArtifact(**response)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            if process.stdin:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            if process.stdout:
                process.stdout.close()


def _exit_when_parent_closes_input() -> None:
    if not sys.stdin.buffer.read(1):
        os._exit(1)


def _child_main() -> None:
    try:
        payload = json.loads(sys.stdin.buffer.readline(65_536))
        threading.Thread(target=_exit_when_parent_closes_input, daemon=True).start()
        policy = ResourcePolicy(**payload["policy"])
        if os.name == "posix":
            import resource

            # This also bounds each spill file. Total spill and RSS are sampled by
            # the parent; native engine buffer limits alone do not cap process RSS.
            resource.setrlimit(resource.RLIMIT_FSIZE, (policy.output_limit_bytes, policy.output_limit_bytes))
        from .native import materialize_in_child

        artifact = materialize_in_child(
            payload["engine"],
            SnapshotRef(Path(payload["input_path"])),
            TransformationPlan(payload["operation"], payload["parameters"]),
            Path(payload["output_path"]),
            Path(payload["spill_path"]),
            policy,
        )
        result = asdict(artifact)
        result["path"] = str(artifact.path)
    except Exception as exc:
        kind = "failure"
        if isinstance(exc, EngineCapabilityError):
            kind = "capability"
        elif isinstance(exc, (EngineResourceLimitError, MemoryError)) or (
            isinstance(exc, OSError) and exc.errno in {errno.EFBIG, errno.ENOSPC}
        ):
            kind = "resource"
        result = {"error": str(exc)[:1000], "kind": kind}
    print(json.dumps(result), flush=True)
    # Avoid waiting for the lifetime-monitor thread's buffered stdin lock during
    # interpreter shutdown. Native connections have already left their contexts.
    os._exit(0 if "error" not in result else 1)


if __name__ == "__main__":
    _child_main()
