from pathlib import Path
import hashlib
import json
import math

import pyarrow.parquet as pq
import pandas as pd
from sqlalchemy.orm import Session
from app.models.chart import Chart
from app.models.dashboard import Dashboard
from app.models.dataset import Dataset
from app.models.project import Project
from app.services.dataset_service import DatasetService
from app.services.chart_cache import chart_cache
from app.services.chart_filters import apply_filters, column_kind
from app.services.chart_query import (
    bounded_result, json_value, metric_result, pie_result, query_gate, query_parquet, required_columns, validate_chart,
)

class DashboardService:
    @staticmethod
    def _drop_nan_aggregates(grouped: pd.Series) -> pd.Series:
        """Remove non-JSON NaN aggregates without dropping numeric zero values."""
        return grouped[grouped.notna()]

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

    @classmethod
    def ensure_dashboard(cls, db: Session, dashboard_id: int, owner_id: int) -> Dashboard:
        dashboard = (
            db.query(Dashboard)
            .join(Project, Project.id == Dashboard.project_id)
            .filter(Dashboard.id == dashboard_id, Project.owner_id == owner_id)
            .first()
        )
        if not dashboard:
            raise ValueError("Dashboard not found.")
        return dashboard

    @classmethod
    def ensure_dataset_for_dashboard(
        cls,
        db: Session,
        dashboard_id: int,
        dataset_id: int,
        owner_id: int,
    ) -> Dataset:
        dashboard = cls.ensure_dashboard(db, dashboard_id, owner_id)
        dataset = (
            db.query(Dataset)
            .join(Project, Project.id == Dataset.project_id)
            .filter(
                Dataset.id == dataset_id,
                Dataset.project_id == dashboard.project_id,
                Dataset.deleted_at.is_(None),
                Project.owner_id == owner_id,
            )
            .first()
        )
        if not dataset:
            raise ValueError("Dataset must belong to the dashboard project.")
        DatasetService.ensure_ready(dataset)
        return dataset

    _apply_filters = staticmethod(apply_filters)

    @staticmethod
    def _cache_key(dataset, chart, owner_id: int) -> str | None:
        # A revision and immutable path identify the logical data. Stat identity
        # additionally prevents stale hits after accidental filesystem changes.
        version = getattr(dataset, "version", None)
        if version is None:
            return None
        path = Path(dataset.stored_path)
        try:
            stat = path.stat()
        except OSError as exc:
            raise ValueError("The dataset snapshot is unavailable.") from exc
        definition = {key: getattr(chart, key) for key in (
            "chart_type", "title", "x_column", "y_column", "aggregation", "filters_json",
        )}
        identity = [owner_id, chart.dataset_id, version, str(path.resolve()),
                    stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, definition]
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()

    @classmethod
    def build_chart_data(cls, db: Session, chart: Chart, owner_id: int) -> dict:
        dataset = cls.ensure_dataset_for_dashboard(
            db,
            chart.dashboard_id,
            chart.dataset_id,
            owner_id,
        )

        key = cls._cache_key(dataset, chart, owner_id)
        if key and (cached := chart_cache.get(key)) is not None:
            return cached
        path = Path(dataset.stored_path)
        with query_gate.acquire():
            if path.suffix.lower() == ".parquet":
                DatasetService.preflight_binary_dataset(path)
                result = query_parquet(path, chart)
                if result is None:
                    columns = required_columns(chart, pq.read_schema(path).names)
                    frame = pd.read_parquet(path, columns=columns)
                    result = cls._build_pandas_chart(frame, chart)
            else:
                frame = DatasetService.read_dataframe(path)
                result = cls._build_pandas_chart(frame, chart)
        normalized: dict = json_value(result)
        if key:
            chart_cache.put(key, normalized)
        return normalized

    @classmethod
    def _build_pandas_chart(cls, df: pd.DataFrame, chart) -> dict:
        validate_chart(chart, {str(c): column_kind(df[c]) for c in df})
        df = cls._apply_filters(df, chart.filters_json or {})

        if chart.chart_type == "table":
            preview = df.head(100).astype(object).where(pd.notna(df.head(100)), None)
            return metric_result(bounded_result([], [], total_count=len(df), rows=preview.to_dict(orient="records")), chart, len(df), len(df))

        if chart.chart_type == "scatter":
            x, y = df[chart.x_column], df[chart.y_column]
            valid = x.map(lambda value: pd.notna(value) and math.isfinite(float(value))) & y.map(
                lambda value: pd.notna(value) and math.isfinite(float(value)))
            total = int(valid.sum())
            return metric_result(bounded_result(json_value(x[valid].head(100).tolist()), json_value(y[valid].head(100).tolist()),
                                  total_count=total, omitted_count=len(df) - total), chart, len(df), total)

        if chart.chart_type == "histogram":
            if not chart.x_column or chart.x_column not in df.columns:
                raise ValueError("Invalid X column.")
            values = df[chart.x_column].dropna()
            if values.empty:
                return metric_result({"labels": [], "values": [], "rows": []}, chart, len(df), 0)
            if any(not math.isfinite(float(value)) for value in values):
                raise ValueError("Histograms require finite numeric values.")
            counts, _bins = pd.cut(values, bins=10, retbins=True)
            grouped = counts.value_counts().sort_index()
            return metric_result({
                "labels": [str(label) for label in grouped.index],
                "values": [int(value) for value in grouped.values],
                "rows": [],
            }, chart, len(df), len(values))

        column = chart.y_column or (chart.x_column if chart.chart_type == "kpi" else None)
        aggregation = chart.aggregation or ("count" if chart.chart_type == "kpi" else "sum")
        if column and aggregation != "count" and chart.chart_type != "pie":
            values = df[column]
            finite = values.map(lambda value: pd.notna(value) and math.isfinite(float(value)))
            if not finite.all():
                df = df.assign(**{column: values.where(finite, None)})
        valid_count = int(df[column].count()) if column else len(df)

        if chart.chart_type == "kpi":
            column = chart.y_column or chart.x_column
            if not column or column not in df.columns:
                raise ValueError("Invalid value column.")
            agg = chart.aggregation or "count"
            series = df[column]
            if agg == "sum" and pd.api.types.is_integer_dtype(series.dtype):
                # NumPy int64 reductions wrap silently; Python integers retain
                # the exact admitted sum, matching DuckDB's wider accumulator.
                series = series.astype(object)
            value = {
                "sum": series.sum,
                "mean": series.mean,
                "count": series.count,
                "min": series.min,
                "max": series.max,
            }[agg]()
            if hasattr(value, "item"):
                value = value.item()
            if pd.isna(value):
                value = None
            return metric_result({"labels": [chart.title], "values": [value], "rows": []}, chart, len(df), valid_count)

        if not chart.x_column or chart.x_column not in df.columns:
            raise ValueError("Invalid X column.")

        if chart.chart_type == "pie":
            if chart.y_column and chart.y_column in df.columns:
                values = df[chart.y_column]
                aggregation = chart.aggregation or "sum"
                if aggregation != "count" and any(not math.isfinite(float(value)) for value in values.dropna()):
                    raise ValueError("Pie charts require finite, non-negative category values.")
                if pd.api.types.is_bool_dtype(values.dtype):
                    df = df.assign(**{chart.y_column: values.astype("Int64")})
                    values = df[chart.y_column]
                if aggregation == "sum" and pd.api.types.is_integer_dtype(values.dtype):
                    df = df.assign(**{chart.y_column: values.astype(object)})
                grouped = (
                    df.groupby(chart.x_column, dropna=False)[chart.y_column]
                    .agg(chart.aggregation or "sum")
                )
                grouped = cls._drop_nan_aggregates(grouped)
            else:
                grouped = df[chart.x_column].astype(str).value_counts()
            return metric_result(pie_result(
                [str(value) for value in grouped.index.tolist()],
                [value.item() if hasattr(value, "item") else value for value in grouped.values],
            ), chart, len(df), valid_count)

        if not chart.y_column or chart.y_column not in df.columns:
            raise ValueError("Invalid Y column.")

        agg = chart.aggregation or "sum"
        if agg == "sum" and pd.api.types.is_integer_dtype(df[chart.y_column].dtype):
            df = df.assign(**{chart.y_column: df[chart.y_column].astype(object)})
        grouped = df.groupby(chart.x_column, dropna=False)[chart.y_column].agg(agg)
        grouped = cls._drop_nan_aggregates(grouped)
        total = len(grouped)
        grouped = grouped.head(100)
        labels = [str(v) for v in grouped.index.tolist()]
        values = [v.item() if hasattr(v, "item") else v for v in grouped.tolist()]
        return metric_result(bounded_result(labels, values, total_count=total), chart, len(df), valid_count)
