from pathlib import Path
from uuid import uuid4

from fastapi import UploadFile

from app.core.config import settings


class LocalStorage:
    """Local storage boundary that can be replaced by an S3-compatible adapter."""

    def __init__(self, root: str | None = None):
        self.root = Path(root or settings.UPLOAD_DIR)
        self.root.mkdir(parents=True, exist_ok=True)

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
            with temporary_path.open("xb") as destination:
                while chunk := await file.read(chunk_size):
                    total_bytes += len(chunk)
                    if total_bytes > max_bytes:
                        raise ValueError("File exceeds the allowed size limit.")
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
        return final_path.with_name(f".{final_path.stem}.part{final_path.suffix}")

    @staticmethod
    def commit_temporary(temporary_path: Path, final_path: Path) -> None:
        temporary_path.replace(final_path)

    @staticmethod
    def delete(path: Path) -> None:
        path.unlink(missing_ok=True)


storage = LocalStorage()
