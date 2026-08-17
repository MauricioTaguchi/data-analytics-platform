from copy import deepcopy
from datetime import datetime, timezone
from io import BufferedRandom, TextIOWrapper
from pathlib import Path
from typing import Callable
from zipfile import BadZipFile, ZipFile

import pandas as pd
from fastapi import UploadFile
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.dataset import Dataset
from app.models.project import Project
from app.models.report import Report
from app.models.transformation import Transformation
from app.models.user import User
from app.services.job_service import (
    JobCancellationRequested,
    JobService,
    JobStateConflict,
)
from app.services.storage_service import storage


ALLOWED_CONTENT_TYPES = {
    ".csv": {"text/csv", "application/csv", "text/plain", "application/vnd.ms-excel"},
    ".xlsx": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
    ".xls": {"application/vnd.ms-excel"},
    ".json": {"application/json", "text/json", "text/plain"},
    ".parquet": {"application/octet-stream", "application/vnd.apache.parquet"},
}
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

    @staticmethod
    def normalize_preview_value(value):
        missing = pd.isna(value)
        if not hasattr(missing, "__len__") and bool(missing):
            return None
        if isinstance(value, (bytes, bytearray, memoryview)):
            return f"<binary value: {len(value)} bytes>"
        if isinstance(value, str) and len(value) > settings.TRANSFORMATION_PREVIEW_MAX_CELL_CHARS:
            limit = settings.TRANSFORMATION_PREVIEW_MAX_CELL_CHARS
            return f"{value[:limit]}..."
        if hasattr(value, "item"):
            try:
                value = value.item()
            except ValueError:
                return str(value)[: settings.TRANSFORMATION_PREVIEW_MAX_CELL_CHARS]
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return value

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
    def ensure_ready(dataset: Dataset) -> None:
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
        if suffix == ".csv":
            return pd.read_csv(path)
        if suffix == ".xlsx":
            DatasetService.preflight_binary_dataset(path)
            return pd.read_excel(path)
        if suffix == ".xls":
            return pd.read_excel(path)
        if suffix == ".json":
            return pd.read_json(path)
        if suffix == ".parquet":
            DatasetService.preflight_binary_dataset(path)
            return pd.read_parquet(path)
        raise ValueError("Unsupported file format.")

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
        except Exception:
            storage.delete(path)
            raise

    @staticmethod
    def apply_operation(df: pd.DataFrame, operation: str, parameters: dict) -> pd.DataFrame:
        result = df.copy(deep=True)
        if operation == "drop_columns":
            columns = parameters.get("columns", [])
            unknown = set(columns) - set(result.columns)
            if unknown:
                raise ValueError(f"Unknown columns: {', '.join(sorted(unknown))}")
            result = result.drop(columns=columns)
        elif operation == "rename_columns":
            mapping = parameters.get("mapping", {})
            unknown = set(mapping) - set(result.columns)
            if unknown:
                raise ValueError(f"Unknown columns: {', '.join(sorted(unknown))}")
            renamed = [mapping.get(column, column) for column in result.columns]
            if len(renamed) != len(set(renamed)):
                raise ValueError("Renaming would create duplicate column names.")
            result = result.rename(columns=mapping)
        elif operation == "fill_nulls":
            values = parameters.get("values", {})
            unknown = set(values) - set(result.columns)
            if unknown:
                raise ValueError(f"Unknown columns: {', '.join(sorted(unknown))}")
            result = result.fillna(values)
        elif operation == "drop_duplicates":
            result = result.drop_duplicates()
        elif operation == "cast_types":
            mapping = parameters.get("mapping", {})
            unknown = set(mapping) - set(result.columns)
            if unknown:
                raise ValueError(f"Unknown columns: {', '.join(sorted(unknown))}")
            for column, dtype in mapping.items():
                result[column] = result[column].astype(dtype)
        else:
            raise ValueError("Unsupported transformation operation.")
        if result.shape[1] == 0:
            raise ValueError("A dataset must keep at least one column.")
        return result

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

            # PostgreSQL serializes admissions for the same account on this
            # row. SQLite's FOR UPDATE is a no-op, so local test/development
            # concurrency remains best-effort rather than a hard guarantee.
            db.query(User.id).filter(User.id == owner_id).with_for_update().one()
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
            return dataset
        except Exception:
            db.rollback()
            storage.delete(path)
            raise

    @classmethod
    def inspect_staged_dataset(cls, dataset: Dataset) -> dict:
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
        dataframe = cls.read_dataframe(Path(dataset.stored_path))
        start = (page - 1) * page_size
        preview_columns = list(dataframe.columns[: settings.TRANSFORMATION_PREVIEW_MAX_COLUMNS])
        page_frame = dataframe.iloc[
            start : start + page_size,
            : settings.TRANSFORMATION_PREVIEW_MAX_COLUMNS,
        ].copy()
        return {
            "columns": [str(column) for column in page_frame.columns],
            "rows": cls.preview_records(page_frame),
            "total_rows": int(dataframe.shape[0]),
            "total_columns": int(dataframe.shape[1]),
            "columns_truncated": max(0, len(dataframe.columns) - len(preview_columns)),
            "page": page,
            "page_size": page_size,
        }

    @classmethod
    def build_profile(cls, dataset: Dataset) -> dict:
        dataframe = cls.read_dataframe(Path(dataset.stored_path))
        cls.validate_dataframe_structure(dataframe)
        all_numeric_columns = dataframe.select_dtypes(include="number")
        numeric_frame = all_numeric_columns.iloc[
            :, : settings.MAX_PROFILE_CORRELATION_COLUMNS
        ]
        total_cells = max(int(dataframe.shape[0] * dataframe.shape[1]), 1)
        columns = []
        for name in dataframe.columns:
            series = dataframe[name]
            item = {
                "name": str(name),
                "dtype": str(series.dtype),
                "missing_count": int(series.isna().sum()),
                "missing_percentage": round(float(series.isna().mean() * 100), 2),
                "unique_count": int(series.nunique(dropna=True)),
            }
            if pd.api.types.is_numeric_dtype(series) and not series.dropna().empty:
                clean = series.dropna()
                q1, q3 = clean.quantile([0.25, 0.75])
                iqr = q3 - q1
                item.update(
                    {
                        "outlier_count": int(
                            clean[(clean < q1 - 1.5 * iqr) | (clean > q3 + 1.5 * iqr)].shape[0]
                        ),
                        "mean": round(float(clean.mean()), 4),
                        "median": round(float(clean.median()), 4),
                    }
                )
            columns.append(item)

        duplicate_rows = int(dataframe.duplicated().sum())
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
            .where(lambda values: values.notna(), None)
            .to_dict()
            if numeric_frame.shape[1] >= 2
            else {}
        )
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
        }

    @classmethod
    def preview_transformation(cls, dataset: Dataset, operation: str, parameters: dict) -> dict:
        cls.ensure_ready(dataset)
        before = cls.read_dataframe(Path(dataset.stored_path))
        after = cls.apply_operation(before, operation, parameters)
        cls.validate_dataframe_structure(after)
        preview_columns = list(after.columns[: settings.TRANSFORMATION_PREVIEW_MAX_COLUMNS])
        sample = after.loc[:, preview_columns].head(20)

        rows = cls.preview_records(sample)
        return {
            "operation": operation,
            "before": {
                "rows": len(before),
                "columns": len(before.columns),
                "missing_cells": int(before.isna().sum().sum()),
            },
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
    ) -> tuple[Transformation, bool]:
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
        output_path = storage.version_path(input_path)
        transformation = Transformation(
            dataset_id=dataset.id,
            user_id=user_id,
            operation=operation,
            parameters=parameters,
            status="pending",
            idempotency_key=idempotency_key,
            expected_version=expected_version,
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
    ) -> Transformation:
        transformation = (
            db.query(Transformation)
            .filter(Transformation.id == transformation_id)
            .first()
        )
        if not transformation:
            raise ValueError("Transformation not found.")
        if transformation.status == "completed":
            return transformation

        owner_id = int(transformation.user_id)
        dataset_id = int(transformation.dataset_id)
        expected_version = int(transformation.expected_version)
        input_path_value = str(transformation.input_path)
        operation = str(transformation.operation)
        parameters = deepcopy(transformation.parameters)
        final_path = Path(transformation.output_path)
        temporary_path = storage.temporary_version_path(final_path)
        final_path_written = False
        try:
            if checkpoint:
                checkpoint()
            dataset = db.query(Dataset).filter(Dataset.id == transformation.dataset_id).first()
            if not dataset:
                raise ValueError("Dataset not found.")
            if (
                dataset.version != transformation.expected_version
                or dataset.stored_path != transformation.input_path
            ):
                raise ValueError("Dataset changed before the transformation could start.")

            transformation.status = "processing"
            db.commit()

            input_path = Path(input_path_value)
            input_size = input_path.stat().st_size
            cls._ensure_transformation_output_size(input_size)
            storage.ensure_capacity(input_size)

            before = cls.read_dataframe(input_path)
            if checkpoint:
                checkpoint()
            after = cls.apply_operation(before, operation, parameters)
            cls.validate_dataframe_structure(after)
            if checkpoint:
                checkpoint()

            # Use a conservative quota snapshot to bound the temporary output
            # without holding a database transaction during serialization.
            cls.ensure_storage_quota(
                db,
                owner_id,
                input_size,
            )
            quota_limit = settings.USER_STORAGE_QUOTA_MB * 1024 * 1024
            quota_remaining = max(
                0,
                quota_limit
                - cls.tracked_storage_usage_bytes(db, owner_id),
            )
            expanded_limit = settings.MAX_DATASET_EXPANDED_SIZE_MB * 1024 * 1024
            db.commit()

            cls.write_dataframe_limited(
                after,
                temporary_path,
                expanded_limit_bytes=expanded_limit,
                quota_remaining_bytes=quota_remaining,
            )
            output_size = temporary_path.stat().st_size
            cls._ensure_transformation_output_size(output_size)

            # Final admission is intentionally short: serialize first, then
            # lock the account and durable job only for quota revalidation,
            # rename, pointer CAS and the caller's final commit.
            db.query(User.id).filter(User.id == owner_id).with_for_update().one()
            if transaction_fence:
                transaction_fence()
            transformation = (
                db.query(Transformation)
                .execution_options(populate_existing=True)
                .filter(Transformation.id == transformation_id)
                .first()
            )
            dataset = (
                db.query(Dataset)
                .execution_options(populate_existing=True)
                .filter(Dataset.id == dataset_id)
                .first()
            )
            if not transformation or not dataset:
                raise ValueError("Transformation target no longer exists.")
            if (
                dataset.version != expected_version
                or dataset.stored_path != input_path_value
            ):
                raise ValueError("Dataset changed while the transformation output was being built.")
            cls.ensure_storage_quota(
                db,
                owner_id,
                output_size,
            )
            # The file already occupies its bytes; this final check verifies
            # that the configured free-space floor still holds before commit.
            storage.ensure_capacity()
            storage.commit_temporary(temporary_path, final_path)
            final_path_written = True

            updated = (
                db.query(Dataset)
                .filter(
                    Dataset.id == dataset.id,
                    Dataset.version == expected_version,
                    Dataset.stored_path == input_path_value,
                )
                .update(
                    {
                        Dataset.stored_path: str(final_path),
                        Dataset.row_count: int(after.shape[0]),
                        Dataset.column_count: int(after.shape[1]),
                        Dataset.profile_json: None,
                        Dataset.version: expected_version + 1,
                        Dataset.status: "ready",
                    },
                    synchronize_session=False,
                )
            )
            if updated != 1:
                raise ValueError("Dataset version conflict detected while committing the transformation.")

            transformation.status = "completed"
            transformation.before_rows = int(before.shape[0])
            transformation.after_rows = int(after.shape[0])
            transformation.before_columns = int(before.shape[1])
            transformation.after_columns = int(after.shape[1])
            transformation.error_message = None
            db.flush()
            db.refresh(transformation)
            return transformation
        except JobCancellationRequested:
            storage.delete(temporary_path)
            if final_path_written:
                storage.delete(final_path)
            db.rollback()
            raise
        except JobStateConflict:
            storage.delete(temporary_path)
            if final_path_written:
                storage.delete(final_path)
            db.rollback()
            raise
        except Exception as exc:
            storage.delete(temporary_path)
            if final_path_written:
                # Delete while the transaction still owns the job fence. A
                # replacement attempt cannot publish to this path until the
                # rollback below releases the row lock.
                storage.delete(final_path)
            db.rollback()
            if checkpoint:
                try:
                    checkpoint()
                except JobCancellationRequested:
                    storage.delete(temporary_path)
                    raise
                except JobStateConflict:
                    storage.delete(temporary_path)
                    raise
            failed = db.query(Transformation).filter(Transformation.id == transformation_id).first()
            current_dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
            if failed:
                failed.status = "failed"
                failed.error_message = str(exc)[:2_000]
            if current_dataset and current_dataset.version == expected_version:
                current_dataset.status = "ready"
            db.commit()
            raise

    @staticmethod
    def undo_last(db: Session, dataset: Dataset, user_id: int) -> Transformation:
        locked_dataset = (
            db.query(Dataset)
            .filter(Dataset.id == dataset.id)
            .with_for_update()
            .first()
        )
        if not locked_dataset:
            raise ValueError("Dataset not found.")
        DatasetService.ensure_ready(locked_dataset)
        transformation = (
            db.query(Transformation)
            .filter(
                Transformation.dataset_id == dataset.id,
                Transformation.user_id == user_id,
                Transformation.status == "completed",
                Transformation.undone_at.is_(None),
            )
            .order_by(Transformation.created_at.desc())
            .with_for_update()
            .first()
        )
        if not transformation:
            raise ValueError("There is no completed transformation to undo.")
        if locked_dataset.version != transformation.expected_version + 1:
            raise ValueError("The latest transformation is no longer the active dataset version.")
        transformation.undone_at = datetime.now(timezone.utc)
        transformation.status = "undone"
        locked_dataset.stored_path = transformation.input_path
        locked_dataset.row_count = transformation.before_rows
        locked_dataset.column_count = transformation.before_columns
        locked_dataset.version = max(1, locked_dataset.version - 1)
        locked_dataset.profile_json = None
        locked_dataset.status = "ready"
        db.commit()
        db.refresh(transformation)
        return transformation
