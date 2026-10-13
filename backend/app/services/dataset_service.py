from contextlib import suppress
from copy import deepcopy
from datetime import datetime, timezone
from io import BufferedRandom, TextIOWrapper
import math
from numbers import Real
from pathlib import Path
from typing import Any, Callable, Protocol
from uuid import uuid4
from zipfile import BadZipFile, ZipFile

import pandas as pd
import numpy as np
from fastapi import UploadFile
from starlette.concurrency import run_in_threadpool
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.dataset import Dataset
from app.models.project import Project
from app.models.report import Report
from app.models.transformation import Transformation
from app.services.job_service import JobService
from app.services.storage_service import storage
from app.services.artifact_service import ArtifactService
from app.services.dataset_audit_service import DatasetAuditService
from app.services.snapshot_service import SnapshotService
from app.services.engines import (
    PandasEngineAdapter, ResourcePolicy, SnapshotRef, TransformationPlan, select_engine,
)


ALLOWED_CONTENT_TYPES = {
    ".csv": {"text/csv", "application/csv", "text/plain", "application/vnd.ms-excel"},
    ".xlsx": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
    ".xls": {"application/vnd.ms-excel"},
    ".json": {"application/json", "text/json", "text/plain"},
    ".parquet": {"application/octet-stream", "application/vnd.apache.parquet"},
}


class StoredSnapshot(Protocol):
    @property
    def stored_path(self) -> str: ...


class ReadySnapshot(StoredSnapshot, Protocol):
    @property
    def status(self) -> str: ...


class UserStorageQuotaError(ValueError):
    """Raised when a staged file would exceed tracked per-user local storage."""


class DatasetExpansionLimitError(ValueError):
    """Raised when binary metadata indicates unsafe decompression expansion."""


class _CapacityGuardedWriter(BufferedRandom):
    """Seekable writer that enforces output limits before each disk write."""

    def __init__(
        self,
        path: Path,
        *,
        expanded_limit_bytes: int,
        quota_remaining_bytes: int,
    ) -> None:
        self._path = path
        self._expanded_limit_bytes = max(0, expanded_limit_bytes)
        self._quota_remaining_bytes = max(0, quota_remaining_bytes)
        self._high_water_mark = 0
        path.parent.mkdir(parents=True, exist_ok=True)
        raw_file = path.open("x+b", buffering=0)
        try:
            super().__init__(raw_file)
        except Exception:
            raw_file.close()
            raise

    def write(self, data) -> int:
        projected_size = max(self._high_water_mark, self.tell() + len(data))
        if projected_size > self._expanded_limit_bytes:
            raise DatasetExpansionLimitError(
                "The transformed dataset exceeds the allowed output size."
            )
        if projected_size > self._quota_remaining_bytes:
            raise UserStorageQuotaError(
                "The transformed dataset would exceed the per-user storage quota."
            )

        try:
            persisted_size = self._path.stat().st_size
        except FileNotFoundError:
            persisted_size = 0
        # Buffered writers may retain earlier chunks in memory. Reserving the
        # full outstanding growth keeps the configured free-space floor intact.
        storage.ensure_capacity(max(0, projected_size - persisted_size))
        written = super().write(data)
        self._high_water_mark = max(self._high_water_mark, self.tell())
        return written


class DatasetService:
    @staticmethod
    def validate_dataframe_structure(dataframe: pd.DataFrame) -> None:
        if dataframe.shape[1] > settings.MAX_DATASET_COLUMNS:
            raise ValueError(
                f"Datasets are limited to {settings.MAX_DATASET_COLUMNS:,} columns."
            )
        if any(
            len(str(column)) > settings.MAX_DATASET_COLUMN_NAME_CHARS
            for column in dataframe.columns
        ):
            raise ValueError(
                "Dataset column names are limited to "
                f"{settings.MAX_DATASET_COLUMN_NAME_CHARS:,} characters."
            )

    @classmethod
    def normalize_preview_value(cls, value):
        # JSON has no NaN/Infinity; normalize only the presentation, never the
        # stored snapshot. Arrow list/struct cells also need recursive handling.
        if isinstance(value, dict):
            return {str(key): cls.normalize_preview_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, np.ndarray)):
            return [cls.normalize_preview_value(item) for item in value]
        missing = pd.isna(value)
        if not hasattr(missing, "__len__") and bool(missing):
            return None
        if isinstance(value, Real) and not math.isfinite(value):
            return None
        if isinstance(value, (bytes, bytearray, memoryview)):
            return f"<binary value: {len(value)} bytes>"
        if isinstance(value, str) and len(value) > settings.TRANSFORMATION_PREVIEW_MAX_CELL_CHARS:
            limit = settings.TRANSFORMATION_PREVIEW_MAX_CELL_CHARS
            return f"{value[:limit]}..."
        if hasattr(value, "item"):
            try:
                return cls.normalize_preview_value(value.item())
            except ValueError:
                return str(value)[: settings.TRANSFORMATION_PREVIEW_MAX_CELL_CHARS]
        if hasattr(value, "isoformat"):
            return value.isoformat()
        if not isinstance(value, (str, int, float, bool, type(None))):
            # Decimal and other scalar extension values retain a readable
            # representation without depending on a framework JSON encoder.
            return str(value)[: settings.TRANSFORMATION_PREVIEW_MAX_CELL_CHARS]
        return value

    @staticmethod
    def _comparison_value(value):
        """Hashable identities for the nested cells accepted by JSON/Parquet."""
        if isinstance(value, dict):
            return ("struct", tuple(sorted(
                (str(key), DatasetService._comparison_value(item)) for key, item in value.items()
            )))
        if isinstance(value, (list, tuple, np.ndarray)):
            return ("list", tuple(DatasetService._comparison_value(item) for item in value))
        missing = pd.isna(value)
        return None if not hasattr(missing, "__len__") and bool(missing) else value

    @classmethod
    def preview_records(cls, dataframe: pd.DataFrame) -> list[dict]:
        return [
            {str(column): cls.normalize_preview_value(value) for column, value in row.items()}
            for row in dataframe.to_dict(orient="records")
        ]

    @staticmethod
    def tracked_storage_usage_bytes(db: Session, owner_id: int) -> int:
        paths = {
            stored_path
            for (stored_path,) in (
                db.query(Dataset.stored_path)
                .join(Project, Project.id == Dataset.project_id)
                .filter(Project.owner_id == owner_id)
                .all()
            )
            if stored_path
        }
        transformation_paths = (
            db.query(Transformation.input_path, Transformation.output_path)
            .join(Dataset, Dataset.id == Transformation.dataset_id)
            .join(Project, Project.id == Dataset.project_id)
            .filter(Project.owner_id == owner_id)
            .all()
        )
        for input_path, output_path in transformation_paths:
            paths.update(path for path in (input_path, output_path) if path)
        paths.update(
            file_path
            for (file_path,) in (
                db.query(Report.file_path)
                .join(Project, Project.id == Report.project_id)
                .filter(Project.owner_id == owner_id)
                .all()
            )
            if file_path
        )

        total_bytes = 0
        for stored_path in paths:
            try:
                path = Path(stored_path)
                if path.is_file():
                    total_bytes += path.stat().st_size
            except OSError:
                # A concurrent cleanup may remove a tracked artifact between
                # discovery and stat. Missing files do not consume quota.
                continue
        return total_bytes

    @classmethod
    def ensure_storage_quota(
        cls,
        db: Session,
        owner_id: int,
        incoming_bytes: int,
    ) -> None:
        quota_bytes = settings.USER_STORAGE_QUOTA_MB * 1024 * 1024
        if cls.tracked_storage_usage_bytes(db, owner_id) + incoming_bytes > quota_bytes:
            raise UserStorageQuotaError(
                "The operation would exceed the per-user storage quota."
            )

    @staticmethod
    def _validate_expansion(expanded_bytes: int, compressed_bytes: int) -> None:
        expanded_limit = settings.MAX_DATASET_EXPANDED_SIZE_MB * 1024 * 1024
        if expanded_bytes > expanded_limit:
            raise DatasetExpansionLimitError(
                "The dataset expands beyond the allowed processing size."
            )
        if expanded_bytes and (
            compressed_bytes <= 0
            or expanded_bytes > compressed_bytes * settings.MAX_DATASET_EXPANSION_RATIO
        ):
            raise DatasetExpansionLimitError(
                "The dataset compression ratio exceeds the allowed limit."
            )

    @classmethod
    def preflight_binary_dataset(cls, path: Path) -> None:
        suffix = path.suffix.lower()
        if suffix == ".xlsx":
            try:
                with ZipFile(path) as workbook:
                    entries = workbook.infolist()
            except (BadZipFile, OSError) as exc:
                raise ValueError("The XLSX archive is invalid.") from exc
            if len(entries) > settings.MAX_XLSX_ARCHIVE_ENTRIES:
                raise DatasetExpansionLimitError(
                    "The XLSX archive contains too many entries."
                )
            if any(entry.flag_bits & 0x1 for entry in entries):
                raise ValueError("Encrypted XLSX files are not supported.")
            cls._validate_expansion(
                sum(entry.file_size for entry in entries),
                sum(entry.compress_size for entry in entries),
            )
        elif suffix == ".parquet":
            try:
                import pyarrow.parquet as parquet

                metadata = parquet.ParquetFile(path).metadata
            except Exception as exc:
                raise ValueError("The Parquet metadata is invalid.") from exc
            if metadata.num_rows > settings.MAX_DATASET_ROWS:
                raise DatasetExpansionLimitError(
                    f"Datasets are limited to {settings.MAX_DATASET_ROWS:,} rows."
                )
            if metadata.num_columns > settings.MAX_DATASET_COLUMNS:
                raise DatasetExpansionLimitError(
                    f"Datasets are limited to {settings.MAX_DATASET_COLUMNS:,} columns."
                )
            expanded_bytes = sum(
                metadata.row_group(index).total_byte_size
                for index in range(metadata.num_row_groups)
            )
            cls._validate_expansion(expanded_bytes, path.stat().st_size)

    @staticmethod
    def ensure_project(db: Session, project_id: int, owner_id: int) -> Project:
        project = db.query(Project).filter(Project.id == project_id, Project.owner_id == owner_id).first()
        if not project:
            raise ValueError("Project not found.")
        return project

    @staticmethod
    def ensure_ready(dataset: ReadySnapshot) -> None:
        if dataset.status not in {"ready", "profiled"}:
            raise ValueError(f"Dataset is not ready. Current status: {dataset.status}.")

    @staticmethod
    def validate_upload_metadata(file: UploadFile) -> str:
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in ALLOWED_CONTENT_TYPES:
            raise ValueError("Use CSV, Excel, JSON, or Parquet.")
        content_type = (file.content_type or "application/octet-stream").lower()
        if content_type not in ALLOWED_CONTENT_TYPES[suffix]:
            raise ValueError("File content type does not match its extension.")
        return suffix

    @staticmethod
    def read_dataframe(path: Path) -> pd.DataFrame:
        suffix = path.suffix.lower()
        row_limit = settings.MAX_DATASET_ROWS
        if suffix == ".csv":
            # Validate the header before allocating rows. nrows bounds parsing
            # while retaining Pandas' whole-column type inference; concatenating
            # independently inferred chunks can silently change mixed columns.
            DatasetService.validate_dataframe_structure(pd.read_csv(path, nrows=0))
            dataframe = pd.read_csv(path, nrows=row_limit + 1)
        elif suffix in {".xlsx", ".xls"}:
            if suffix == ".xlsx":
                DatasetService.preflight_binary_dataset(path)
            with pd.ExcelFile(path) as workbook:
                DatasetService.validate_dataframe_structure(pd.read_excel(workbook, nrows=0))
                dataframe = pd.read_excel(workbook, nrows=row_limit + 1)
        elif suffix == ".json":
            # The existing JSON contract accepts arrays and column-oriented
            # objects, not just JSONL. Keep that contract; this parser is eager
            # and remains subject to upload/worker limits.
            dataframe = pd.read_json(path)
        elif suffix == ".parquet":
            DatasetService.preflight_binary_dataset(path)
            dataframe = pd.read_parquet(path)
        else:
            raise ValueError("Unsupported file format.")
        DatasetService.validate_dataframe_structure(dataframe)
        if len(dataframe) > row_limit:
            raise DatasetExpansionLimitError(f"Datasets are limited to {row_limit:,} rows.")
        return dataframe

    @staticmethod
    def write_dataframe(df: pd.DataFrame, path: Path) -> None:
        suffix = path.suffix.lower()
        if suffix == ".csv":
            df.to_csv(path, index=False)
        elif suffix in {".xlsx", ".xls"}:
            df.to_excel(path, index=False)
        elif suffix == ".json":
            df.to_json(path, orient="records", force_ascii=False)
        elif suffix == ".parquet":
            df.to_parquet(path, index=False)
        else:
            raise ValueError("Unsupported file format.")

    @staticmethod
    def _ensure_transformation_output_size(output_bytes: int) -> None:
        expanded_limit = settings.MAX_DATASET_EXPANDED_SIZE_MB * 1024 * 1024
        if output_bytes > expanded_limit:
            raise DatasetExpansionLimitError(
                "The transformed dataset exceeds the allowed output size."
            )

    @classmethod
    def write_dataframe_limited(
        cls,
        df: pd.DataFrame,
        path: Path,
        *,
        expanded_limit_bytes: int,
        quota_remaining_bytes: int,
    ) -> None:
        """Serialize a transformation without allowing unbounded disk growth."""

        try:
            with _CapacityGuardedWriter(
                path,
                expanded_limit_bytes=expanded_limit_bytes,
                quota_remaining_bytes=quota_remaining_bytes,
            ) as destination:
                suffix = path.suffix.lower()
                if suffix in {".csv", ".json"}:
                    text_destination = TextIOWrapper(
                        destination,
                        encoding="utf-8",
                        newline="",
                    )
                    try:
                        if suffix == ".csv":
                            df.to_csv(text_destination, index=False)
                        else:
                            df.to_json(
                                text_destination,
                                orient="records",
                                force_ascii=False,
                            )
                        text_destination.flush()
                    finally:
                        # The outer context owns the binary stream. Detaching
                        # also flushes any remaining encoded text through the
                        # same guarded write path.
                        text_destination.detach()
                elif suffix in {".xlsx", ".xls"}:
                    df.to_excel(destination, index=False, engine="openpyxl")
                elif suffix == ".parquet":
                    df.to_parquet(destination, index=False, engine="pyarrow")
                else:
                    raise ValueError("Unsupported file format.")
                destination.flush()
            if path.suffix.lower() == ".parquet":
                # A compressed artifact must remain readable under the same
                # expansion/shape limits used by previews and later operations.
                cls.preflight_binary_dataset(path)
        except Exception:
            storage.delete(path)
            raise

    @staticmethod
    def apply_operation(
        df: pd.DataFrame, operation: str, parameters: dict, *, version: str | None = None,
    ) -> pd.DataFrame:
        from app.services.operations import execute_operation

        return execute_operation(df, operation, parameters, version=version)

    @classmethod
    async def stage_upload(
        cls,
        db: Session,
        project_id: int,
        owner_id: int,
        file: UploadFile,
        capacity_task_id: str | None = None,
    ) -> Dataset:
        cls.ensure_project(db, project_id, owner_id)
        suffix = cls.validate_upload_metadata(file)
        JobService.ensure_capacity(
            db,
            owner_id,
            exclude_task_id=capacity_task_id,
        )
        max_bytes = settings.MAX_FILE_SIZE_MB * 1024 * 1024
        chunk_size = settings.UPLOAD_CHUNK_SIZE_MB * 1024 * 1024
        path, uploaded_bytes = await storage.stage_upload(
            file,
            suffix,
            max_bytes,
            chunk_size,
        )
        try:
            if suffix in {".csv", ".json"}:
                with path.open("rb") as staged_file:
                    if b"\x00" in staged_file.read(1024):
                        raise ValueError("The uploaded text file contains null bytes.")
            cls.preflight_binary_dataset(path)
            uploaded_identity = await run_in_threadpool(SnapshotService.digest, path)

            # PostgreSQL serializes admissions for the same account on this
            # row. SQLite's FOR UPDATE is a no-op, so local test/development
            # concurrency remains best-effort rather than a hard guarantee.
            JobService.lock_owner(db, owner_id)
            JobService.ensure_capacity(
                db,
                owner_id,
                exclude_task_id=capacity_task_id,
            )
            cls.ensure_storage_quota(db, owner_id, uploaded_bytes)

            dataset = Dataset(
                project_id=project_id,
                original_filename=file.filename or path.name,
                stored_path=str(path),
                status="queued",
                version=1,
            )
            db.add(dataset)
            db.flush()
            db.refresh(dataset)
            uploaded_identity.assert_unchanged(path)
            SnapshotService.record(
                db, dataset_id=dataset.id, path=path, version=1,
                size_bytes=uploaded_identity.size_bytes, sha256=uploaded_identity.sha256,
            )
            return dataset
        except Exception:
            db.rollback()
            storage.delete(path)
            raise

    @classmethod
    def inspect_staged_dataset(cls, dataset: StoredSnapshot) -> dict:
        path = Path(dataset.stored_path)
        try:
            dataframe = cls.read_dataframe(path)
        except Exception as exc:
            raise ValueError(f"Could not read the file: {exc}") from exc
        cls.validate_dataframe_structure(dataframe)
        if dataframe.shape[0] > settings.MAX_DATASET_ROWS:
            raise ValueError(f"Datasets are limited to {settings.MAX_DATASET_ROWS:,} rows.")
        return {"rows": int(dataframe.shape[0]), "columns": int(dataframe.shape[1])}

    @classmethod
    def preview(cls, dataset: Dataset, page: int, page_size: int) -> dict:
        cls.ensure_ready(dataset)
        path = Path(dataset.stored_path)
        start = (page - 1) * page_size
        if path.suffix.lower() == ".parquet":
            import pyarrow as pa
            import pyarrow.parquet as pq

            cls.preflight_binary_dataset(path)
            with pq.ParquetFile(path) as source:
                total_rows = source.metadata.num_rows
                total_columns = len(source.schema_arrow.names)
                preview_columns = source.schema_arrow.names[: settings.TRANSFORMATION_PREVIEW_MAX_COLUMNS]
                selected_batches = []
                remaining = page_size
                group_start = 0
                for index in range(source.metadata.num_row_groups):
                    group_rows = source.metadata.row_group(index).num_rows
                    group_end = group_start + group_rows
                    if group_end > start and group_start < start + page_size:
                        skip = max(0, start - group_start)
                        for batch in source.iter_batches(
                            batch_size=min(4096, page_size), row_groups=[index], columns=preview_columns,
                        ):
                            if skip >= batch.num_rows:
                                skip -= batch.num_rows
                                continue
                            selected = batch.slice(skip, min(remaining, batch.num_rows - skip))
                            selected_batches.append(selected)
                            remaining -= selected.num_rows
                            skip = 0
                            if remaining == 0:
                                break
                    group_start = group_end
                    if remaining == 0 or group_start >= start + page_size:
                        break
                page_frame = (
                    pa.Table.from_batches(selected_batches).to_pandas()
                    if selected_batches else pd.DataFrame(columns=preview_columns)
                )
        else:
            dataframe = cls.read_dataframe(path)
            total_rows, total_columns = dataframe.shape
            preview_columns = list(dataframe.columns[: settings.TRANSFORMATION_PREVIEW_MAX_COLUMNS])
            page_frame = dataframe.iloc[
                start : start + page_size, : settings.TRANSFORMATION_PREVIEW_MAX_COLUMNS,
            ].copy()
        return {
            "columns": [str(column) for column in page_frame.columns],
            "rows": cls.preview_records(page_frame),
            "total_rows": int(total_rows),
            "total_columns": int(total_columns),
            "columns_truncated": max(0, total_columns - len(preview_columns)),
            "page": page,
            "page_size": page_size,
        }

    @classmethod
    def build_profile(cls, dataset: StoredSnapshot) -> dict:
        dataframe = cls.read_dataframe(Path(dataset.stored_path))
        cls.validate_dataframe_structure(dataframe)
        all_numeric_columns = dataframe.select_dtypes(include="number")
        numeric_frame = all_numeric_columns.iloc[
            :, : settings.MAX_PROFILE_CORRELATION_COLUMNS
        ].replace([np.inf, -np.inf], np.nan)
        total_cells = max(int(dataframe.shape[0] * dataframe.shape[1]), 1)
        columns: list[dict[str, Any]] = []
        for name in dataframe.columns:
            series = dataframe[name]
            missing_count = int(series.isna().sum())
            try:
                unique_count = int(series.nunique(dropna=True))
            except TypeError:
                unique_count = int(series.map(cls._comparison_value).nunique(dropna=True))
            item: dict[str, Any] = {
                "name": str(name),
                "dtype": str(series.dtype),
                "missing_count": missing_count,
                "missing_percentage": round(missing_count / max(len(series), 1) * 100, 2),
                "unique_count": unique_count,
            }
            if pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series):
                present = series.dropna()
                clean = present[np.isfinite(present)]
                outlier_count = 0
                if not clean.empty:
                    q1, q3 = clean.quantile([0.25, 0.75])
                    iqr = q3 - q1
                    outlier_count = int(((clean < q1 - 1.5 * iqr) | (clean > q3 + 1.5 * iqr)).sum())
                item.update(
                    {
                        "finite_count": int(len(clean)),
                        "non_finite_count": int(len(present) - len(clean)),
                        "outlier_count": outlier_count,
                        "mean": cls.normalize_preview_value(round(float(clean.mean()), 4)) if len(clean) else None,
                        "median": cls.normalize_preview_value(round(float(clean.median()), 4)) if len(clean) else None,
                    }
                )
            columns.append(item)

        try:
            duplicate_rows = int(dataframe.duplicated().sum())
        except TypeError:
            duplicate_rows = int(dataframe.map(cls._comparison_value).duplicated().sum())
        missing_cells = int(dataframe.isna().sum().sum())
        quality_score = max(
            0,
            round(
                100
                - (missing_cells / total_cells * 60)
                - (duplicate_rows / max(len(dataframe), 1) * 40)
            ),
        )
        suggestions = (["Remove duplicate rows."] if duplicate_rows else []) + [
            f"Review column '{item['name']}' because more than 20% of its values are missing."
            for item in columns
            if item["missing_percentage"] > 20
        ]
        correlations = (
            numeric_frame.corr(numeric_only=True)
            .round(4)
            .to_dict()
            if numeric_frame.shape[1] >= 2
            else {}
        )
        correlations = {
            str(column): {str(other): cls.normalize_preview_value(value) for other, value in values.items()}
            for column, values in correlations.items()
        }
        return {
            "summary": {
                "rows": int(dataframe.shape[0]),
                "columns": int(dataframe.shape[1]),
                "duplicate_rows": duplicate_rows,
                "missing_cells": missing_cells,
                "missing_percentage": round(missing_cells / total_cells * 100, 2),
                "quality_score": quality_score,
            },
            "schema": {str(column): str(dtype) for column, dtype in dataframe.dtypes.items()},
            "columns": columns,
            "correlations": correlations,
            "correlation_columns_analyzed": int(numeric_frame.shape[1]),
            "correlation_columns_truncated": max(
                0,
                int(all_numeric_columns.shape[1] - numeric_frame.shape[1]),
            ),
            "suggestions": suggestions,
            "numeric_statistics_policy": "Means, medians, outliers and correlations use finite values only; undefined statistics are null. Missing counts exclude infinities.",
        }

    @classmethod
    def preview_transformation(
        cls, dataset: ReadySnapshot, operation: str, parameters: dict, *, engine_name: str | None = None,
        output_path: Path | None = None, checkpoint: Callable[[], object] | None = None,
        quota_remaining_bytes: int | None = None, operation_version: str | None = None,
        _child_adapter=None,
    ) -> dict:
        from app.services.operations import validate_operation_capability

        cls.ensure_ready(dataset)
        selected_engine = engine_name or settings.TRANSFORMATION_ENGINE
        plan = validate_operation_capability(
            operation, parameters,
            engine_name=selected_engine,
            input_path=Path(dataset.stored_path), version=operation_version,
        )
        if output_path is not None and _child_adapter is None:
            from app.services.engines.runner import run_preview

            return run_preview(
                SnapshotRef(Path(dataset.stored_path)), plan,
                ResourcePolicy.configured(quota_remaining_bytes=quota_remaining_bytes), checkpoint,
                engine_name=selected_engine, output_path=output_path,
            )
        before = cls.read_dataframe(Path(dataset.stored_path))
        before_summary = {
            "rows": len(before),
            "columns": len(before.columns),
            "missing_cells": int(before.isna().sum().sum()),
        }
        if output_path is not None:
            del before
            adapter = _child_adapter or select_engine(selected_engine, pandas=PandasEngineAdapter())
            adapter.materialize(
                SnapshotRef(Path(dataset.stored_path)), plan, output_path,
                ResourcePolicy(
                    max_output_bytes=settings.MAX_DATASET_EXPANDED_SIZE_MB * 1024 * 1024,
                    quota_remaining_bytes=(
                        quota_remaining_bytes if quota_remaining_bytes is not None
                        else settings.USER_STORAGE_QUOTA_MB * 1024 * 1024
                    ),
                    threads=settings.TRANSFORMATION_ENGINE_THREADS,
                    memory_limit_bytes=settings.TRANSFORMATION_ENGINE_MEMORY_MB * 1024 * 1024,
                    max_spill_bytes=settings.TRANSFORMATION_ENGINE_SPILL_MB * 1024 * 1024,
                    timeout_seconds=settings.TRANSFORMATION_ENGINE_TIMEOUT_SECONDS,
                    max_columns=settings.MAX_DATASET_COLUMNS,
                    max_column_name_chars=settings.MAX_DATASET_COLUMN_NAME_CHARS,
                    max_rows=settings.MAX_DATASET_ROWS,
                ),
                checkpoint,
            )
            # The task owns this durable temporary reservation and its cleanup.
            after = cls.read_dataframe(output_path)
        else:
            # Kept for direct callers that only need an in-memory Pandas preview.
            after = cls.apply_operation(before, operation, dict(plan.parameters), version=plan.operation_version)
        cls.validate_dataframe_structure(after)
        preview_columns = list(after.columns[: settings.TRANSFORMATION_PREVIEW_MAX_COLUMNS])
        sample = after.loc[:, preview_columns].head(20)

        rows = cls.preview_records(sample)
        return {
            "operation": operation,
            "before": before_summary,
            "after": {
                "rows": len(after),
                "columns": len(after.columns),
                "missing_cells": int(after.isna().sum().sum()),
            },
            "preview_columns": [str(column) for column in preview_columns],
            "columns_truncated": max(0, len(after.columns) - len(preview_columns)),
            "rows": rows,
        }

    @classmethod
    def prepare_transformation(
        cls,
        db: Session,
        dataset: Dataset,
        operation: str,
        parameters: dict,
        user_id: int,
        expected_version: int,
        idempotency_key: str,
        engine_name: str | None = None,
    ) -> tuple[Transformation, bool]:
        # Take the account lock before either the dataset mutation or the
        # transformation FK flush, matching transformation publication.
        JobService.lock_owner(db, user_id)
        locked_dataset = (
            db.query(Dataset)
            .execution_options(populate_existing=True)
            .join(Project, Project.id == Dataset.project_id)
            .filter(
                Dataset.id == dataset.id,
                Dataset.deleted_at.is_(None),
                Project.owner_id == user_id,
            )
            .with_for_update(of=Dataset)
            .first()
        )
        if locked_dataset is None:
            raise ValueError("Dataset not found.")
        dataset = locked_dataset
        existing = (
            db.query(Transformation)
            .execution_options(populate_existing=True)
            .filter(
                Transformation.dataset_id == dataset.id,
                Transformation.user_id == user_id,
                Transformation.idempotency_key == idempotency_key,
            )
            .first()
        )
        if existing:
            if (
                existing.operation != operation
                or existing.parameters != parameters
                or existing.expected_version != expected_version
                or (engine_name is not None and existing.engine_name != engine_name)
            ):
                raise ValueError(
                    "Idempotency-Key has already been used with a different transformation request."
                )
            return existing, False

        cls.ensure_ready(dataset)
        if dataset.version != expected_version:
            raise ValueError(
                f"Dataset version changed from {expected_version} to {dataset.version}. Refresh and try again."
            )

        input_path = Path(dataset.stored_path)
        from app.services.operations import validate_operation_capability

        resolved_engine = engine_name or settings.TRANSFORMATION_ENGINE
        plan = validate_operation_capability(
            operation, parameters, engine_name=resolved_engine, input_path=input_path,
        )
        output_path = storage.version_path(input_path)
        transformation = Transformation(
            dataset_id=dataset.id,
            user_id=user_id,
            operation=operation,
            parameters=parameters,
            status="pending",
            idempotency_key=idempotency_key,
            expected_version=expected_version,
            engine_name=resolved_engine,
            operation_version=plan.operation_version,
            input_path=str(input_path),
            output_path=str(output_path),
            before_rows=int(dataset.row_count or 0),
            after_rows=int(dataset.row_count or 0),
            before_columns=int(dataset.column_count or 0),
            after_columns=int(dataset.column_count or 0),
        )
        dataset.status = "transforming"
        db.add(transformation)
        try:
            db.flush()
            db.refresh(transformation)
            return transformation, True
        except IntegrityError:
            # A concurrent request may have inserted the same idempotency key.
            db.rollback()
            existing = (
                db.query(Transformation)
                .filter(
                    Transformation.dataset_id == dataset.id,
                    Transformation.user_id == user_id,
                    Transformation.idempotency_key == idempotency_key,
                )
                .first()
            )
            if existing:
                if (
                    existing.operation != operation
                    or existing.parameters != parameters
                    or existing.expected_version != expected_version
                    or (engine_name is not None and existing.engine_name != engine_name)
                ):
                    raise ValueError(
                        "Idempotency-Key has already been used with a different transformation request."
                    ) from None
                return existing, False
            raise

    @classmethod
    def execute_prepared_transformation(
        cls,
        db: Session,
        transformation_id: int,
        checkpoint: Callable[[], object] | None = None,
        transaction_fence: Callable[[], object] | None = None,
        *,
        attempt_token: str | None = None,
    ) -> Transformation:
        transformation = (
            db.query(Transformation).execution_options(populate_existing=True)
            .filter(Transformation.id == transformation_id).first()
        )
        if not transformation:
            raise ValueError("Transformation not found.")
        if transformation.status in {"completed", "undone"}:
            return transformation
        owner_id = int(transformation.user_id)
        dataset_id = int(transformation.dataset_id)
        expected_version = int(transformation.expected_version)
        input_path_value = str(transformation.input_path)
        operation = str(transformation.operation)
        operation_version = str(transformation.operation_version)
        parameters = deepcopy(transformation.parameters)
        engine_name = str(transformation.engine_name)
        task_id = transformation.task_id
        if not task_id:
            raise ValueError("A transformation requires a durable job before execution.")
        attempt_token = attempt_token or uuid4().hex
        prepared_path = Path(transformation.output_path)
        suffix = ".parquet"
        # Each attempt owns different bytes, including after the rename. A stale
        # worker can never overwrite/delete the output of its replacement.
        final_path = prepared_path.with_name(f"{uuid4().hex}{suffix}")
        temporary_path = storage.temporary_version_path(final_path)
        try:
            if checkpoint:
                checkpoint()
            dataset = (db.query(Dataset).execution_options(populate_existing=True)
                       .filter(Dataset.id == dataset_id).first())
            if (not dataset or dataset.deleted_at is not None
                    or dataset.version != expected_version or dataset.stored_path != input_path_value):
                raise ValueError("Dataset changed before the transformation could start.")
            # Persist the reservation before creating either path. The collector
            # can finish cleanup even when the process dies before error handling.
            reservation = ArtifactService.reserve(
                db, owner_id=owner_id, task_id=task_id, attempt_token=attempt_token,
                final_path=final_path, temporary_path=temporary_path,
            )
            reservation_id = reservation.id
            db.commit()
            input_path = Path(input_path_value)
            input_size = input_path.stat().st_size
            cls._ensure_transformation_output_size(input_size)
            storage.ensure_capacity(input_size)
            cls.ensure_storage_quota(db, owner_id, input_size)
            quota_remaining = max(0, settings.USER_STORAGE_QUOTA_MB * 1024 * 1024
                                  - cls.tracked_storage_usage_bytes(db, owner_id))
            db.commit()
            adapter = select_engine(engine_name, pandas=PandasEngineAdapter())
            artifact = adapter.materialize(
                SnapshotRef(input_path), TransformationPlan(operation, parameters, operation_version), temporary_path,
                ResourcePolicy(
                    max_output_bytes=settings.MAX_DATASET_EXPANDED_SIZE_MB * 1024 * 1024,
                    quota_remaining_bytes=quota_remaining,
                    threads=settings.TRANSFORMATION_ENGINE_THREADS,
                    memory_limit_bytes=settings.TRANSFORMATION_ENGINE_MEMORY_MB * 1024 * 1024,
                    max_spill_bytes=settings.TRANSFORMATION_ENGINE_SPILL_MB * 1024 * 1024,
                    timeout_seconds=settings.TRANSFORMATION_ENGINE_TIMEOUT_SECONDS,
                    max_columns=settings.MAX_DATASET_COLUMNS,
                    max_column_name_chars=settings.MAX_DATASET_COLUMN_NAME_CHARS,
                    max_rows=settings.MAX_DATASET_ROWS,
                ),
                checkpoint,
            )
            cls._ensure_transformation_output_size(artifact.size_bytes)
            # Consistent order: account -> job -> dataset -> artifact. No parse,
            # engine execution or broker I/O runs while these locks are held.
            JobService.lock_owner(db, owner_id)
            JobService.ensure_active(db, task_id, attempt_token=attempt_token)
            if transaction_fence:
                transaction_fence()
            dataset = (db.query(Dataset).execution_options(populate_existing=True)
                       .filter(Dataset.id == dataset_id).with_for_update().first())
            transformation = (db.query(Transformation).execution_options(populate_existing=True)
                              .filter(Transformation.id == transformation_id).with_for_update().first())
            if (not transformation or not dataset or dataset.deleted_at is not None
                    or dataset.version != expected_version or dataset.stored_path != input_path_value
                    or transformation.status in {"completed", "undone", "cancelled"}):
                raise ValueError("Dataset changed while the transformation output was being built.")
            cls.ensure_storage_quota(db, owner_id, artifact.size_bytes)
            storage.ensure_capacity()
            # This row lock arbitrates publication versus garbage collection.
            # LIVE, the new dataset head and SUCCESS commit together in the caller.
            ArtifactService.publish(db, reservation_id, attempt_token=attempt_token)
            storage.commit_temporary(temporary_path, final_path)
            SnapshotService.record(
                db, dataset_id=dataset_id, path=final_path, version=expected_version + 1,
                size_bytes=artifact.size_bytes, sha256=artifact.sha256,
            )
            updated = (db.query(Dataset).filter(
                Dataset.id == dataset_id, Dataset.version == expected_version,
                Dataset.stored_path == input_path_value, Dataset.deleted_at.is_(None),
            ).update({
                Dataset.stored_path: str(final_path), Dataset.row_count: artifact.rows,
                Dataset.column_count: artifact.columns, Dataset.profile_json: None,
                Dataset.version: expected_version + 1, Dataset.status: "ready",
            }, synchronize_session=False))
            if updated != 1:
                raise ValueError("Dataset version conflict detected while committing the transformation.")
            transformation.output_path = str(final_path)
            transformation.status = "completed"
            transformation.before_rows = artifact.before_rows
            transformation.after_rows = artifact.rows
            transformation.before_columns = artifact.before_columns
            transformation.after_columns = artifact.columns
            transformation.error_message = None
            db.flush()
            db.refresh(transformation)
            DatasetAuditService.record_transformation(db, transformation, artifact=artifact)
            return transformation
        except Exception:
            db.rollback()
            # Rollback acknowledgement may be ambiguous. Never unlink a final
            # output here; its reservation is reconciled against committed state.
            with suppress(OSError):
                storage.delete(temporary_path)
            raise

    @staticmethod
    def undo_last(
        db: Session,
        dataset: Dataset,
        user_id: int,
        *,
        expected_version: int,
        idempotency_key: str | None = None,
    ) -> dict:
        if idempotency_key is not None and (not idempotency_key.strip() or len(idempotency_key) > 120):
            raise ValueError("Idempotency-Key must contain 1 to 120 characters.")
        dataset_id = dataset.id
        observed = (db.query(Dataset).execution_options(populate_existing=True)
                    .join(Project, Project.id == Dataset.project_id)
                    .filter(Dataset.id == dataset_id, Dataset.deleted_at.is_(None), Project.owner_id == user_id)
                    .first())
        if observed is None:
            raise ValueError("Dataset not found.")
        fingerprint = DatasetAuditService.fingerprint(
            kind="undo", dataset_id=dataset_id, expected_version=expected_version,
        )
        receipt = DatasetAuditService.replay(
            db, dataset_id=dataset_id, actor_id=user_id, kind="undo",
            idempotency_key=idempotency_key, request_fingerprint=fingerprint,
        )
        if receipt is not None:
            db.rollback()
            return receipt
        DatasetService.ensure_ready(observed)
        if observed.version != expected_version:
            raise ValueError("Dataset version changed. Refresh and try again.")
        candidate = (db.query(Transformation)
                     .join(Dataset, Dataset.id == Transformation.dataset_id)
                     .join(Project, Project.id == Dataset.project_id)
                     .filter(Dataset.id == dataset_id, Dataset.deleted_at.is_(None),
                             Project.owner_id == user_id, Dataset.version == expected_version,
                             Transformation.status == "completed", Transformation.undone_at.is_(None),
                             Transformation.output_path == Dataset.stored_path)
                     .order_by(Transformation.id.desc()).first())
        if candidate is None:
            raise ValueError("There is no completed transformation for the active dataset version to undo.")
        candidate_id, restored_path = candidate.id, str(candidate.input_path)
        verified_file, manifest_id = SnapshotService.verify(db, dataset_id, restored_path)
        # Hashing runs without row locks. Fence the exact head, transformation,
        # manifest and file identity before committing the restored pointer.
        JobService.lock_owner(db, user_id)
        locked_dataset = (
            db.query(Dataset)
            .execution_options(populate_existing=True)
            .join(Project, Project.id == Dataset.project_id)
            .filter(
                Dataset.id == dataset_id,
                Dataset.deleted_at.is_(None),
                Project.owner_id == user_id,
            )
            .with_for_update(of=Dataset)
            .first()
        )
        if not locked_dataset:
            raise ValueError("Dataset not found.")
        fingerprint = DatasetAuditService.fingerprint(
            kind="undo", dataset_id=locked_dataset.id, expected_version=expected_version,
        )
        receipt = DatasetAuditService.replay(
            db, dataset_id=locked_dataset.id, actor_id=user_id, kind="undo",
            idempotency_key=idempotency_key, request_fingerprint=fingerprint,
        )
        if receipt is not None:
            # Release admission locks; a replay is a read of its original result.
            db.rollback()
            return receipt
        if locked_dataset.version != expected_version:
            raise ValueError("Dataset version changed. Refresh and try again.")
        DatasetService.ensure_ready(locked_dataset)
        transformation = (
            db.query(Transformation)
            .execution_options(populate_existing=True)
            .filter(
                Transformation.dataset_id == locked_dataset.id,
                Transformation.user_id == user_id,
                Transformation.status == "completed",
                Transformation.undone_at.is_(None),
                Transformation.output_path == locked_dataset.stored_path,
            )
            .order_by(Transformation.id.desc())
            .with_for_update()
            .first()
        )
        if not transformation:
            raise ValueError("There is no completed transformation for the active dataset to undo.")
        if transformation.id != candidate_id or transformation.input_path != restored_path:
            raise ValueError("Dataset changed during snapshot verification. Refresh and try again.")
        manifest = SnapshotService.manifest(db, dataset_id, restored_path, lock=True)
        if (manifest.id if manifest else None) != manifest_id or (manifest and manifest.availability != "AVAILABLE"):
            raise ValueError("The previous snapshot expired during verification; the active dataset was not changed.")
        try:
            verified_file.assert_unchanged(restored_path)
        except OSError as exc:
            raise ValueError("The previous snapshot is unavailable; the active dataset was not changed.") from exc
        transformation.undone_at = datetime.now(timezone.utc)
        transformation.status = "undone"
        locked_dataset.stored_path = transformation.input_path
        locked_dataset.row_count = transformation.before_rows
        locked_dataset.column_count = transformation.before_columns
        # Restoring older content is a new revision. Never reuse a revision
        # observed by a queued worker or by a previous client request.
        locked_dataset.version += 1
        locked_dataset.profile_json = None
        locked_dataset.status = "ready"
        receipt = DatasetAuditService.record_undo(
            db, transformation, actor_id=user_id, previous_version=expected_version,
            idempotency_key=idempotency_key, request_fingerprint=fingerprint,
            snapshot_integrity="verified" if manifest_id else "unverified", snapshot_id=manifest_id,
        )
        db.commit()
        # No database/cache read after commit: a lost acknowledgement is resolved
        # by replaying the immutable receipt with the same idempotency key.
        return receipt
