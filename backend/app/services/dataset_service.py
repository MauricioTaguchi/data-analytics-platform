from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from fastapi import UploadFile
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.dataset import Dataset
from app.models.project import Project
from app.models.transformation import Transformation
from app.services.storage_service import storage


ALLOWED_CONTENT_TYPES = {
    ".csv": {"text/csv", "application/csv", "text/plain", "application/vnd.ms-excel"},
    ".xlsx": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
    ".xls": {"application/vnd.ms-excel"},
    ".json": {"application/json", "text/json", "text/plain"},
    ".parquet": {"application/octet-stream", "application/vnd.apache.parquet"},
}


class DatasetService:
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
        if suffix in {".xlsx", ".xls"}:
            return pd.read_excel(path)
        if suffix == ".json":
            return pd.read_json(path)
        if suffix == ".parquet":
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
    ) -> Dataset:
        cls.ensure_project(db, project_id, owner_id)
        suffix = cls.validate_upload_metadata(file)
        max_bytes = settings.MAX_FILE_SIZE_MB * 1024 * 1024
        chunk_size = settings.UPLOAD_CHUNK_SIZE_MB * 1024 * 1024
        path, _ = await storage.stage_upload(file, suffix, max_bytes, chunk_size)

        if suffix in {".csv", ".json"}:
            with path.open("rb") as staged_file:
                if b"\x00" in staged_file.read(1024):
                    storage.delete(path)
                    raise ValueError("The uploaded text file contains null bytes.")

        dataset = Dataset(
            project_id=project_id,
            original_filename=file.filename or path.name,
            stored_path=str(path),
            status="queued",
            version=1,
        )
        try:
            db.add(dataset)
            db.commit()
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
        if dataframe.shape[1] > 1_000:
            raise ValueError("Datasets are limited to 1,000 columns.")
        if dataframe.shape[0] > settings.MAX_DATASET_ROWS:
            raise ValueError(f"Datasets are limited to {settings.MAX_DATASET_ROWS:,} rows.")
        return {"rows": int(dataframe.shape[0]), "columns": int(dataframe.shape[1])}

    @classmethod
    def preview(cls, dataset: Dataset, page: int, page_size: int) -> dict:
        cls.ensure_ready(dataset)
        dataframe = cls.read_dataframe(Path(dataset.stored_path))
        start = (page - 1) * page_size
        page_frame = dataframe.iloc[start : start + page_size].copy()
        page_frame = page_frame.where(pd.notna(page_frame), None)
        return {
            "columns": [str(column) for column in page_frame.columns],
            "rows": page_frame.to_dict(orient="records"),
            "total_rows": int(dataframe.shape[0]),
            "page": page,
            "page_size": page_size,
        }

    @classmethod
    def build_profile(cls, dataset: Dataset) -> dict:
        dataframe = cls.read_dataframe(Path(dataset.stored_path))
        numeric_frame = dataframe.select_dtypes(include="number")
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
            "suggestions": suggestions,
        }

    @classmethod
    def preview_transformation(cls, dataset: Dataset, operation: str, parameters: dict) -> dict:
        cls.ensure_ready(dataset)
        before = cls.read_dataframe(Path(dataset.stored_path))
        after = cls.apply_operation(before, operation, parameters)
        sample = after.head(20).where(pd.notna(after.head(20)), None)
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
            "rows": sample.to_dict(orient="records"),
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
            db.commit()
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
                return existing, False
            raise

    @classmethod
    def execute_prepared_transformation(cls, db: Session, transformation_id: int) -> Transformation:
        transformation = (
            db.query(Transformation)
            .filter(Transformation.id == transformation_id)
            .first()
        )
        if not transformation:
            raise ValueError("Transformation not found.")
        if transformation.status == "completed":
            return transformation

        final_path = Path(transformation.output_path)
        temporary_path = storage.temporary_version_path(final_path)
        try:
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

            before = cls.read_dataframe(Path(transformation.input_path))
            after = cls.apply_operation(before, transformation.operation, transformation.parameters)
            cls.write_dataframe(after, temporary_path)
            storage.commit_temporary(temporary_path, final_path)

            updated = (
                db.query(Dataset)
                .filter(
                    Dataset.id == dataset.id,
                    Dataset.version == transformation.expected_version,
                    Dataset.stored_path == transformation.input_path,
                )
                .update(
                    {
                        Dataset.stored_path: str(final_path),
                        Dataset.row_count: int(after.shape[0]),
                        Dataset.column_count: int(after.shape[1]),
                        Dataset.profile_json: None,
                        Dataset.version: transformation.expected_version + 1,
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
            db.commit()
            db.refresh(transformation)
            return transformation
        except Exception as exc:
            db.rollback()
            storage.delete(temporary_path)
            storage.delete(final_path)
            failed = db.query(Transformation).filter(Transformation.id == transformation_id).first()
            current_dataset = db.query(Dataset).filter(Dataset.id == transformation.dataset_id).first()
            if failed:
                failed.status = "failed"
                failed.error_message = str(exc)[:2_000]
            if current_dataset and current_dataset.version == transformation.expected_version:
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
