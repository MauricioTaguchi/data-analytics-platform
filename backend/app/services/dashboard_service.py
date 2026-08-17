from pathlib import Path
import pandas as pd
from sqlalchemy.orm import Session
from app.models.chart import Chart
from app.models.dashboard import Dashboard
from app.models.dataset import Dataset
from app.models.project import Project
from app.services.dataset_service import DatasetService

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

    @staticmethod
    def _apply_filters(df: pd.DataFrame, filters: dict) -> pd.DataFrame:
        filtered = df.copy()
        for column, rule in filters.items():
            if column not in filtered.columns:
                continue
            if isinstance(rule, dict):
                if "eq" in rule:
                    filtered = filtered[filtered[column] == rule["eq"]]
                if "gte" in rule:
                    filtered = filtered[filtered[column] >= rule["gte"]]
                if "lte" in rule:
                    filtered = filtered[filtered[column] <= rule["lte"]]
                if "contains" in rule:
                    filtered = filtered[
                        filtered[column]
                        .astype(str)
                        .str.contains(
                            str(rule["contains"]),
                            case=False,
                            na=False,
                            regex=False,
                        )
                    ]
        return filtered

    @classmethod
    def build_chart_data(cls, db: Session, chart: Chart, owner_id: int) -> dict:
        dataset = cls.ensure_dataset_for_dashboard(
            db,
            chart.dashboard_id,
            chart.dataset_id,
            owner_id,
        )

        df = DatasetService.read_dataframe(Path(dataset.stored_path))
        df = cls._apply_filters(df, chart.filters_json or {})

        if chart.chart_type == "table":
            preview = df.head(100).where(pd.notna(df), None)
            return {"labels": [], "values": [], "rows": preview.to_dict(orient="records")}

        if chart.chart_type == "histogram":
            if not chart.x_column or chart.x_column not in df.columns:
                raise ValueError("Invalid X column.")
            counts, _bins = pd.cut(df[chart.x_column].dropna(), bins=10, retbins=True)
            grouped = counts.value_counts().sort_index()
            return {
                "labels": [str(label) for label in grouped.index],
                "values": [int(value) for value in grouped.values],
                "rows": [],
            }

        if chart.chart_type == "kpi":
            column = chart.y_column or chart.x_column
            if not column or column not in df.columns:
                raise ValueError("Invalid value column.")
            agg = chart.aggregation or "count"
            series = df[column]
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
            return {"labels": [chart.title], "values": [value], "rows": []}

        if not chart.x_column or chart.x_column not in df.columns:
            raise ValueError("Invalid X column.")

        if chart.chart_type == "pie":
            if chart.y_column and chart.y_column in df.columns:
                grouped = (
                    df.groupby(chart.x_column, dropna=False)[chart.y_column]
                    .agg(chart.aggregation or "sum")
                )
                grouped = cls._drop_nan_aggregates(grouped).head(20)
            else:
                grouped = df[chart.x_column].astype(str).value_counts().head(20)
            return {
                "labels": [str(value) for value in grouped.index.tolist()],
                "values": [value.item() if hasattr(value, "item") else value for value in grouped.values],
                "rows": [],
            }

        if not chart.y_column or chart.y_column not in df.columns:
            raise ValueError("Invalid Y column.")

        agg = chart.aggregation or "sum"
        grouped = df.groupby(chart.x_column, dropna=False)[chart.y_column].agg(agg)
        grouped = cls._drop_nan_aggregates(grouped).head(100)
        labels = [str(v) for v in grouped.index.tolist()]
        values = [v.item() if hasattr(v, "item") else v for v in grouped.tolist()]
        return {"labels": labels, "values": values, "rows": []}
