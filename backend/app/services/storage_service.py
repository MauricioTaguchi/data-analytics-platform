from pathlib import Path
from shutil import disk_usage
from uuid import uuid4

from fastapi import UploadFile

from app.core.config import settings


class UploadSizeLimitError(ValueError):
    """Raised when a streamed upload exceeds its configured byte limit."""


class StorageCapacityError(RuntimeError):
    """Raised when local storage cannot retain the configured free-space floor."""


class LocalStorage:
    """Local storage boundary that can be replaced by an S3-compatible adapter."""

    def __init__(self, root: str | None = None):
        self.root = Path(root or settings.UPLOAD_DIR)
        self.root.mkdir(parents=True, exist_ok=True)

    def available_bytes(self) -> int:
        return int(disk_usage(self.root).free)

    @staticmethod
    def ensure_path_capacity(path: Path, required_bytes: int = 0) -> None:
        minimum_free_bytes = settings.MIN_FREE_DISK_SPACE_MB * 1024 * 1024
        capacity_path = path if path.is_dir() else path.parent
        capacity_path.mkdir(parents=True, exist_ok=True)
        if int(disk_usage(capacity_path).free) - max(0, required_bytes) < minimum_free_bytes:
            raise StorageCapacityError(
                "The operation cannot be accepted because local storage is low on space."
            )

    def ensure_capacity(self, required_bytes: int = 0) -> None:
        self.ensure_path_capacity(self.root, required_bytes)

    async def stage_upload(
        self,
        file: UploadFile,
        suffix: str,
        max_bytes: int,
        chunk_size: int,
    ) -> tuple[Path, int]:
        identifier = uuid4().hex
        temporary_path = self.root / f".{identifier}{suffix}.part"
        final_path = self.root / f"{identifier}{suffix}"
        total_bytes = 0

        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self.ensure_capacity()
            with temporary_path.open("xb") as destination:
                while chunk := await file.read(chunk_size):
                    total_bytes += len(chunk)
                    if total_bytes > max_bytes:
                        raise UploadSizeLimitError("File exceeds the allowed size limit.")
                    # This is admission control, not a filesystem reservation;
                    # rechecking for every chunk narrows the concurrent-write race.
                    self.ensure_capacity(len(chunk))
                    destination.write(chunk)
            if total_bytes == 0:
                raise ValueError("The uploaded file is empty.")
            temporary_path.replace(final_path)
            return final_path, total_bytes
        except Exception:
            temporary_path.unlink(missing_ok=True)
            final_path.unlink(missing_ok=True)
            raise
        finally:
            await file.close()

    def version_path(self, source: Path) -> Path:
        suffix = ".xlsx" if source.suffix.lower() == ".xls" else source.suffix
        return self.root / f"{source.stem}.v-{uuid4().hex[:10]}{suffix}"

    def temporary_version_path(self, final_path: Path) -> Path:
        # Keep the data format as the final suffix so dataframe writers can
        # select the correct serializer while the file is still temporary.
        # A unique name prevents a stale worker from touching a newer
        # attempt's in-flight artifact after its lease has expired.
        return final_path.with_name(
            f".{final_path.stem}.{uuid4().hex}.part{final_path.suffix}"
        )

    @staticmethod
    def commit_temporary(temporary_path: Path, final_path: Path) -> None:
        temporary_path.replace(final_path)

    @staticmethod
    def delete(path: Path) -> None:
        path.unlink(missing_ok=True)


storage = LocalStorage()
