from pathlib import Path
from uuid import uuid4
import json
import pandas as pd
from fastapi import UploadFile
from sqlalchemy.orm import Session
from app.core.config import settings
from app.models.dataset import Dataset
from app.models.project import Project
from app.models.transformation import Transformation

ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".xls", ".json", ".parquet"}

class DatasetService:
    @staticmethod
    def ensure_project(db: Session, project_id: int, owner_id: int) -> Project:
        project = (
            db.query(Project)
            .filter(Project.id == project_id, Project.owner_id == owner_id)
            .first()
        )
        if not project:
            raise ValueError("Project not found.")
        return project

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

    @classmethod
    async def save_upload(
        cls,
        db: Session,
        project_id: int,
        owner_id: int,
        file: UploadFile,
    ) -> Dataset:
        cls.ensure_project(db, project_id, owner_id)
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in ALLOWED_EXTENSIONS:
            raise ValueError("Use CSV, Excel, JSON ou Parquet.")

        content = await file.read()
        if len(content) > settings.MAX_FILE_SIZE_MB * 1024 * 1024:
            raise ValueError("File exceeds the allowed size limit.")

        upload_dir = Path(settings.UPLOAD_DIR)
        upload_dir.mkdir(parents=True, exist_ok=True)
        path = upload_dir / f"{uuid4().hex}{suffix}"
        path.write_bytes(content)

        try:
            df = cls.read_dataframe(path)
        except Exception as exc:
            path.unlink(missing_ok=True)
            raise ValueError(f"Falha ao ler o arquivo: {exc}") from exc

        dataset = Dataset(
            project_id=project_id,
            original_filename=file.filename or path.name,
            stored_path=str(path),
            row_count=int(df.shape[0]),
            column_count=int(df.shape[1]),
            status="ready",
        )
        db.add(dataset)
        db.commit()
        db.refresh(dataset)
        return dataset

    @classmethod
    def preview(cls, dataset: Dataset, page: int, page_size: int) -> dict:
        df = cls.read_dataframe(Path(dataset.stored_path))
        start = (page - 1) * page_size
        page_df = df.iloc[start:start + page_size].copy()
        page_df = page_df.where(pd.notna(page_df), None)
        return {
            "columns": [str(c) for c in page_df.columns],
            "rows": page_df.to_dict(orient="records"),
            "total_rows": int(df.shape[0]),
            "page": page,
            "page_size": page_size,
        }

    @classmethod
    def build_profile(cls, dataset: Dataset) -> dict:
        df = cls.read_dataframe(Path(dataset.stored_path))
        numeric_df = df.select_dtypes(include="number")
        total_cells = max(int(df.shape[0] * df.shape[1]), 1)

        columns = []
        for name in df.columns:
            series = df[name]
            profile = {
                "name": str(name),
                "dtype": str(series.dtype),
                "missing_count": int(series.isna().sum()),
                "missing_percentage": round(float(series.isna().mean() * 100), 2),
                "unique_count": int(series.nunique(dropna=True)),
            }
            if pd.api.types.is_numeric_dtype(series):
                clean = series.dropna()
                if not clean.empty:
                    q1, q3 = clean.quantile([0.25, 0.75])
                    iqr = q3 - q1
                    outliers = clean[(clean < q1 - 1.5 * iqr) | (clean > q3 + 1.5 * iqr)]
                    profile["outlier_count"] = int(outliers.shape[0])
                    profile["mean"] = round(float(clean.mean()), 4)
                    profile["median"] = round(float(clean.median()), 4)
            columns.append(profile)

        correlations = {}
        if numeric_df.shape[1] >= 2:
            correlations = (
                numeric_df.corr(numeric_only=True)
                .round(4)
                .where(lambda x: x.notna(), None)
                .to_dict()
            )

        suggestions = []
        if df.duplicated().sum() > 0:
            suggestions.append("Remove duplicate rows.")
        for item in columns:
            if item["missing_percentage"] > 20:
                suggestions.append(
                    f"Revisar a coluna '{item['name']}' por excesso de valores ausentes."
                )
            if item.get("outlier_count", 0) > 0:
                suggestions.append(
                    f"Investigar outliers na coluna '{item['name']}'."
                )

        return {
            "summary": {
                "rows": int(df.shape[0]),
                "columns": int(df.shape[1]),
                "duplicate_rows": int(df.duplicated().sum()),
                "missing_cells": int(df.isna().sum().sum()),
                "missing_percentage": round(
                    float(df.isna().sum().sum() / total_cells * 100),
                    2,
                ),
            },
            "columns": columns,
            "correlations": correlations,
            "suggestions": suggestions,
        }

    @classmethod
    def apply_transformation(
        cls,
        db: Session,
        dataset: Dataset,
        operation: str,
        parameters: dict,
    ) -> Transformation:
        path = Path(dataset.stored_path)
        df = cls.read_dataframe(path)

        if operation == "drop_columns":
            df = df.drop(columns=parameters.get("columns", []), errors="ignore")
        elif operation == "rename_columns":
            df = df.rename(columns=parameters.get("mapping", {}))
        elif operation == "fill_nulls":
            values = parameters.get("values", {})
            df = df.fillna(values)
        elif operation == "drop_duplicates":
            df = df.drop_duplicates()
        elif operation == "cast_types":
            for column, dtype in parameters.get("mapping", {}).items():
                df[column] = df[column].astype(dtype)
        else:
            raise ValueError("Unsupported transformation operation.")

        if path.suffix.lower() == ".csv":
            df.to_csv(path, index=False)
        elif path.suffix.lower() in {".xlsx", ".xls"}:
            df.to_excel(path, index=False)
        elif path.suffix.lower() == ".json":
            df.to_json(path, orient="records", force_ascii=False)
        elif path.suffix.lower() == ".parquet":
            df.to_parquet(path, index=False)

        dataset.row_count = int(df.shape[0])
        dataset.column_count = int(df.shape[1])
        dataset.profile_json = None

        transformation = Transformation(
            dataset_id=dataset.id,
            operation=operation,
            parameters=parameters,
        )
        db.add(transformation)
        db.commit()
        db.refresh(transformation)
        return transformation
