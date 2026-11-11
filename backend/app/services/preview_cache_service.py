"""Small presentation cache; originals remain the analytical source of truth.

The browser requests the first 100 rows. Caching exactly that bounded view avoids
another whole-file inference without duplicating an admitted dataset. Later API
pages and incompatible caches use the existing supervised parser.
"""
from functools import lru_cache
from hashlib import sha256
from importlib.metadata import version
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pandas as pd

from app.core.config import settings

CACHE_VERSION = 1
CACHE_ROWS = 100
CACHE_MAX_BYTES = 256 * 1024


def _encoded(payload: dict) -> bytes | None:
    """Bound allocation as well as the eventual database document."""
    chunks: list[bytes] = []
    size = 0
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    for chunk in encoder.iterencode(payload):
        value = chunk.encode("utf-8")
        size += len(value)
        if size > CACHE_MAX_BYTES:
            return None
        chunks.append(value)
    return b"".join(chunks)


@lru_cache(maxsize=1)
def _pandas_version() -> str:
    # A cache hit need not import NumPy/Pandas or initialize their thread pools.
    return version("pandas")


def _contract() -> dict:
    return {
        "version": CACHE_VERSION,
        "pandas": _pandas_version(),
        "max_rows": settings.MAX_DATASET_ROWS,
        "max_columns": settings.MAX_DATASET_COLUMNS,
        "max_column_name_chars": settings.MAX_DATASET_COLUMN_NAME_CHARS,
        "preview_columns": settings.TRANSFORMATION_PREVIEW_MAX_COLUMNS,
        "preview_cell_chars": settings.TRANSFORMATION_PREVIEW_MAX_CELL_CHARS,
        "max_expanded_mb": settings.MAX_DATASET_EXPANDED_SIZE_MB,
        "max_expansion_ratio": settings.MAX_DATASET_EXPANSION_RATIO,
        "max_xlsx_entries": settings.MAX_XLSX_ARCHIVE_ENTRIES,
    }


class PreviewCacheService:
    @staticmethod
    def build(dataframe: "pd.DataFrame", *, source_sha256: str, source_size_bytes: int) -> dict | None:
        from app.services.dataset_service import DatasetService

        page = dataframe.iloc[:CACHE_ROWS, :settings.TRANSFORMATION_PREVIEW_MAX_COLUMNS]
        body = {
            "contract": _contract(),
            "source_sha256": source_sha256,
            "source_size_bytes": source_size_bytes,
            "columns": [str(column) for column in page.columns],
            "rows": DatasetService.preview_records(page),
            "total_rows": int(len(dataframe)),
            "total_columns": int(len(dataframe.columns)),
            "columns_truncated": max(0, len(dataframe.columns) - len(page.columns)),
        }
        encoded = _encoded(body)
        if encoded is None:
            # Cache admission is optional. Do not reject an otherwise valid
            # dataset just because a wide/nested presentation exceeds 256 KiB.
            return None
        return {"body": body, "sha256": sha256(encoded).hexdigest()}

    @staticmethod
    def metadata(snapshot) -> dict | None:
        if (snapshot is None or snapshot.availability != "AVAILABLE"
                or not snapshot.preview_cache_sha256 or not snapshot.preview_cache_json):
            return None
        return {
            "body": snapshot.preview_cache_json,
            "sha256": snapshot.preview_cache_sha256,
            "source_sha256": snapshot.sha256,
            "source_size_bytes": snapshot.size_bytes,
        }

    @staticmethod
    def page(metadata: dict, page: int, page_size: int) -> dict[str, Any] | None:
        body = metadata.get("body")
        if not isinstance(body, dict):
            return None
        try:
            encoded = _encoded(body)
        except (TypeError, ValueError):
            return None
        if (encoded is None or sha256(encoded).hexdigest() != metadata.get("sha256")
                or body.get("contract") != _contract()
                or body.get("source_sha256") != metadata.get("source_sha256")
                or body.get("source_size_bytes") != metadata.get("source_size_bytes")):
            return None
        rows = body.get("rows")
        total_rows = body.get("total_rows")
        columns = body.get("columns")
        if (not isinstance(rows, list) or not isinstance(total_rows, int) or total_rows < 0
                or not isinstance(columns, list) or len(rows) != min(total_rows, CACHE_ROWS)
                or not all(isinstance(row, dict) for row in rows)):
            return None
        start = (page - 1) * page_size
        stop = min(start + page_size, total_rows)
        if start < total_rows and stop > len(rows):
            return None
        return {
            "columns": columns,
            "rows": rows[start:stop],
            "total_rows": total_rows,
            "total_columns": body["total_columns"],
            "columns_truncated": body["columns_truncated"],
            "page": page,
            "page_size": page_size,
        }


    @staticmethod
    def read_verified_page(path: Path, metadata: dict, page: int, page_size: int) -> dict | None:
        """The child's fast path imports neither a dataframe parser nor a DB driver."""
        from app.services.file_integrity import digest_file

        result = PreviewCacheService.page(metadata, page, page_size)
        if result is None:
            return None
        identity = digest_file(path)
        if (identity.sha256 != metadata.get("source_sha256")
                or identity.size_bytes != metadata.get("source_size_bytes")):
            raise ValueError("The dataset snapshot failed integrity verification.")
        identity.assert_unchanged(path)
        after = digest_file(path)
        if (after.sha256, after.size_bytes) != (identity.sha256, identity.size_bytes):
            raise ValueError("The dataset snapshot failed integrity verification.")
        return result
