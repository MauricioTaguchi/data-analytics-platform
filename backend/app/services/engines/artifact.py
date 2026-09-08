import hashlib
from pathlib import Path

from .contracts import EngineResourceLimitError, ResourcePolicy


def validate_paths(input_path: Path, output_path: Path) -> None:
    if input_path.resolve() == output_path.resolve():
        raise ValueError("Transformations must write a new artifact.")
    if output_path.exists() or output_path.is_symlink():
        raise ValueError("Output artifact already exists.")
    if not input_path.is_file():
        raise ValueError("Snapshot file does not exist.")


def artifact_digest(path: Path, policy: ResourcePolicy) -> tuple[int, str]:
    size = path.stat().st_size
    if size > policy.output_limit_bytes:
        raise EngineResourceLimitError("The transformed dataset exceeds its output or storage budget.")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return size, digest.hexdigest()
