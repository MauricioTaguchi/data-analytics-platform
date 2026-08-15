from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field

class DashboardCreate(BaseModel):
    project_id: int
    name: str = Field(min_length=2, max_length=160)
    description: str | None = None
    layout_json: dict[str, Any] = Field(default_factory=dict)

class DashboardResponse(DashboardCreate):
    id: int
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)

class ChartCreate(BaseModel):
    dataset_id: int
    title: str = Field(min_length=2, max_length=160)
    chart_type: Literal["bar", "line", "pie", "histogram", "scatter", "table", "kpi"]
    x_column: str | None = None
    y_column: str | None = None
    aggregation: Literal["sum", "mean", "count", "min", "max"] | None = None
    filters_json: dict[str, Any] = Field(default_factory=dict)

class ChartResponse(ChartCreate):
    id: int
    dashboard_id: int
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)

class ChartDataResponse(BaseModel):
    labels: list[Any]
    values: list[Any]
    rows: list[dict[str, Any]] = Field(default_factory=list)
