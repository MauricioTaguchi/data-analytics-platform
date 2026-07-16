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
                        filtered[column].astype(str).str.contains(str(rule["contains"]), case=False, na=False)
                    ]
        return filtered

    @classmethod
    def build_chart_data(cls, db: Session, chart: Chart, owner_id: int) -> dict:
        dashboard = cls.ensure_dashboard(db, chart.dashboard_id, owner_id)
        dataset = (
            db.query(Dataset)
            .join(Project)
            .filter(Dataset.id == chart.dataset_id, Project.owner_id == owner_id)
            .first()
        )
        if not dataset:
            raise ValueError("Dataset not found.")

        df = DatasetService.read_dataframe(Path(dataset.stored_path))
        df = cls._apply_filters(df, chart.filters_json or {})

        if chart.chart_type == "table":
            preview = df.head(100).where(pd.notna(df), None)
            return {"labels": [], "values": [], "rows": preview.to_dict(orient="records")}

        if chart.chart_type == "histogram":
            if not chart.x_column or chart.x_column not in df.columns:
                raise ValueError("Coluna X inválida.")
            counts, bins = pd.cut(df[chart.x_column].dropna(), bins=10, retbins=True)
            grouped = counts.value_counts().sort_index()
            return {
                "labels": [str(label) for label in grouped.index],
                "values": [int(value) for value in grouped.values],
                "rows": [],
            }

        if chart.chart_type == "kpi":
            column = chart.y_column or chart.x_column
            if not column or column not in df.columns:
                raise ValueError("Coluna inválida.")
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
            return {"labels": [chart.title], "values": [value], "rows": []}

        if not chart.x_column or chart.x_column not in df.columns:
            raise ValueError("Coluna X inválida.")

        if chart.chart_type == "pie":
            grouped = df[chart.x_column].astype(str).value_counts().head(20)
            return {
                "labels": grouped.index.tolist(),
                "values": [int(v) for v in grouped.values],
                "rows": [],
            }

        if not chart.y_column or chart.y_column not in df.columns:
            raise ValueError("Coluna Y inválida.")

        agg = chart.aggregation or "sum"
        grouped = df.groupby(chart.x_column, dropna=False)[chart.y_column].agg(agg).head(100)
        labels = [str(v) for v in grouped.index.tolist()]
        values = [v.item() if hasattr(v, "item") else v for v in grouped.tolist()]
        return {"labels": labels, "values": values, "rows": []}
